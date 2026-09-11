"""
telegram_review.py
-------------------
Camada de aprovação humana via Telegram, opt-in via config.json ('telegram_review' /
'selecao_tema_telegram'). Não usa nenhuma lib de bot (python-telegram-bot etc.) — só
'requests' puro contra a Bot API do Telegram, no mesmo estilo do resto do pipeline
(pesquisar_videos_pexels, buscar_imagem_wikimedia...), pra não adicionar dependência
nova ao requirements.txt.

SECRETS NECESSÁRIOS (env vars / GitHub Actions secrets):
  TELEGRAM_BOT_TOKEN — token do bot, gerado pelo @BotFather no Telegram
  TELEGRAM_CHAT_ID   — chat_id de destino. Pra descobrir o seu: mande QUALQUER
                        mensagem pro bot recém-criado, depois abra no navegador
                        https://api.telegram.org/bot<SEU_TOKEN>/getUpdates — o campo
                        "message":{"chat":{"id": ...}} é o número que você quer.

Se qualquer uma das duas variáveis não estiver setada, ATIVA_TELEGRAM fica False e
todo o resto deste módulo vira no-op silencioso — nada quebra pra quem não configurou
(o pipeline segue 100% automático, igual antes desse módulo existir).

FLUXO DE REVISÃO DE MÍDIA (revisar_midia_pipeline):
  pra cada clipe, na ordem: manda a mídia + o trecho do roteiro correspondente +
  [Aprovar]/[Recusar]
    - Aprovar → segue pro próximo clipe
    - Recusar → manda [Cancelar workflow]/[Enviar mídia]
        - Cancelar → levanta WorkflowCanceladoPeloUsuario (main() captura e encerra
          sem publicar nada)
        - Enviar mídia → espera a PRÓXIMA mensagem: se for um link do Pexels
          (pexels.com/video/... ou /photo/...), baixa aquele item específico pela API;
          se for uma foto/vídeo/documento enviado direto do celular, baixa o arquivo do
          Telegram — em ambos os casos substitui o 'path' do clipe (o timing do corte
          não muda) e segue pro próximo

FLUXO DE SELEÇÃO DE TEMA (escolher_tema_telegram):
  manda uma pergunta com botão [Nada a sugerir]; se a resposta vier como TEXTO, isso
  vira o tema/direcionamento do próximo roteiro; se vier o botão (ou estourar o
  timeout), retorna None e quem chamou cai pra escolha automática de sempre
  (escolher_tema_reflexao(), dentro de generate_video.py).

Ambos os fluxos são SÍNCRONOS/bloqueantes (long-polling no getUpdates) — aceitável
porque o pipeline já roda como um script linear (GitHub Actions ou local), mas atenção
ao timeout do job/runner: se o timeout de resposta configurado for maior que o timeout
do job, o job morre no meio da espera. Ajuste 'timeout_resposta_min' no config.json de
acordo com o timeout do seu runner.
"""

import os
import re
import time
import json

import requests
from PIL import Image, ImageOps
from rede_utils import com_watchdog

TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
ATIVA_TELEGRAM = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

PEXELS_API_KEY = os.environ.get('PEXELS_API_KEY')

_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else None


class WorkflowCanceladoPeloUsuario(Exception):
    """Levantada quando o usuário aperta 'Cancelar workflow' no Telegram (ou não
    responde ao cancelamento a tempo). main() deve capturar isso especificamente e
    encerrar SEM publicar/subir nada — não é uma falha do pipeline, é uma decisão."""
    pass


def _config():
    try:
        with open('config.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _timeout_min(chave, padrao):
    return int(_config().get(chave, {}).get('timeout_resposta_min', padrao))


# ============================================================
# Primitivas da Bot API
# ============================================================

def _chamar(metodo, **params):
    resp = _requisitar_com_retry('post', f"{_API_BASE}/{metodo}", data=params, timeout=30)
    dados = resp.json()
    if not dados.get('ok'):
        raise RuntimeError(f"Telegram API '{metodo}' falhou: {dados}")
    return dados['result']


def _requisitar_com_retry(verbo, url, tentativas=3, espera_s=3, **kwargs):
    """
    BUGFIX (Telegram parou de interagir no meio do workflow): getUpdates é uma conexão
    de long-polling — só UMA pode estar "ativa" por bot de cada vez. Se uma run anterior
    for cancelada manualmente (ex: no GitHub Actions) enquanto uma requisição de
    getUpdates está em aberto, o Telegram pode devolver 409 Conflict ("terminated by
    other getUpdates request") pras primeiras chamadas da run seguinte, até a conexão
    antiga expirar de vez do lado do servidor do Telegram. Sem isso, uma única 409
    (ou qualquer erro de rede transitório) subia sem tratamento e travava a interação
    pro resto do workflow. Agora tenta de novo, com espera progressiva, antes de desistir.
    """
    ultimo_erro = None
    for tentativa in range(1, tentativas + 1):
        try:
            resp = requests.request(verbo, url, **kwargs)
            if resp.status_code == 409:
                raise requests.exceptions.HTTPError(f"409 Conflict em {url}", response=resp)
            resp.raise_for_status()
            return resp
        except Exception as e:
            ultimo_erro = e
            if tentativa < tentativas:
                print(f"    ⚠️ Telegram API falhou (tentativa {tentativa}/{tentativas}: {e}) "
                      f"— tentando de novo em {espera_s}s...")
                time.sleep(espera_s * tentativa)
    raise ultimo_erro


def _enviar_arquivo(metodo, campo_arquivo, caminho, **params):
    # lê o arquivo pra memória ANTES de tentar — se _requisitar_com_retry precisar
    # tentar de novo (ex: depois de um 409), reabrir o arquivo a cada tentativa
    # evitaria o bug de mandar corpo vazio na 2ª tentativa (stream já consumido)
    with open(caminho, 'rb') as f:
        conteudo = f.read()
    nome_arquivo = os.path.basename(caminho)
    resp = _requisitar_com_retry('post', f"{_API_BASE}/{metodo}", data=params,
                                  files={campo_arquivo: (nome_arquivo, conteudo)}, timeout=120)
    dados = resp.json()
    if not dados.get('ok'):
        raise RuntimeError(f"Telegram API '{metodo}' falhou: {dados}")
    return dados['result']


def enviar_texto(texto, botoes=None):
    """botoes: lista de (texto_botao, callback_data) — vira UMA linha de botões inline."""
    params = {'chat_id': TELEGRAM_CHAT_ID, 'text': texto}
    if botoes:
        params['reply_markup'] = json.dumps({
            'inline_keyboard': [[{'text': t, 'callback_data': d} for t, d in botoes]]
        })
    return _chamar('sendMessage', **params)


def enviar_midia(caminho, legenda, botoes=None):
    """Envia foto (jpg/png) ou vídeo (mp4/mov) com legenda + botões inline. Formato não
    reconhecido cai pra mensagem de texto (nunca derruba a revisão por causa disso)."""
    params = {'chat_id': TELEGRAM_CHAT_ID, 'caption': legenda[:1024]}
    if botoes:
        params['reply_markup'] = json.dumps({
            'inline_keyboard': [[{'text': t, 'callback_data': d} for t, d in botoes]]
        })
    ext = os.path.splitext(caminho)[1].lower()
    try:
        if ext in ('.jpg', '.jpeg', '.png'):
            return _enviar_arquivo('sendPhoto', 'photo', caminho, **params)
        elif ext in ('.mp4', '.mov'):
            return _enviar_arquivo('sendVideo', 'video', caminho, **params)
        else:
            return enviar_texto(f"{legenda}\n\n(mídia em formato sem preview: {ext})", botoes)
    except Exception as e:
        print(f"  ⚠️ Falha ao enviar mídia pro Telegram ({e}) — enviando só o texto")
        return enviar_texto(legenda, botoes)


_offset_updates = None
_offset_inicializado = False

# BUGFIX (mídia trocada/perdida/vazando pra outro segmento): antes, cada função de
# espera (aguardar_callback / aguardar_midia_ou_texto) só reconhecia o TIPO de resposta
# que ela mesma esperava naquele instante — um clique de botão OU uma mídia, nunca os
# dois, e só se chegasse durante a janela exata em que aquela função estava rodando.
# Qualquer mensagem que chegasse "fora de hora" (ex: usuário manda a foto de
# substituição antes de clicar Recusar→Enviar mídia, ou manda o próximo link enquanto
# o bot ainda está processando o clipe anterior) era descartada pra sempre — o
# getUpdates do Telegram não devolve a mesma mensagem duas vezes depois que o offset
# passa por ela. Isso é o que causava clipe pulado, ordem trocada, e mídia de um
# segmento vazando pro próximo (a mensagem "atrasada" ficava pendente e era capturada
# pelo primeiro aguardar_... do segmento SEGUINTE).
#
# A correção: TODA atualização que chega é imediatamente classificada e guardada numa
# fila (callback ou mensagem) — nunca descartada. Cada função de espera primeiro olha
# se já tem algo pendente na fila certa antes de fazer long-polling por algo novo. Isso
# também permite mandar tudo em sequência sem esperar o bot perguntar de novo a cada
# clipe (que é como a maioria das pessoas natural mente usa isso).
_fila_callbacks = []
_fila_mensagens = []


def _pasta_downloads_padrao():
    d = os.path.join('assets', 'telegram_review')
    os.makedirs(d, exist_ok=True)
    return d


def limpar_filas_pendentes():
    """Descarta (com aviso) qualquer callback/mensagem que tenha sobrado sem ser
    consumido no passo anterior. Chamado no INÍCIO da revisão de CADA segmento — é o
    que impede uma resposta atrasada do segmento anterior de vazar pro de agora."""
    global _fila_callbacks, _fila_mensagens
    if _fila_callbacks or _fila_mensagens:
        aviso = (f"🧹 Descartando {len(_fila_callbacks)} clique(s) e "
                 f"{len(_fila_mensagens)} mensagem(ns) que sobraram sem uso do "
                 f"segmento anterior (chegaram atrasadas — se era uma mídia pra um "
                 f"clipe específico, manda de novo quando eu pedir).")
        print(f"  {aviso}")
        try:
            enviar_texto(aviso)
        except Exception:
            pass
    _fila_callbacks = []
    _fila_mensagens = []


def _descartar_atualizacoes_antigas():
    """Roda uma vez, na primeira chamada de qualquer fluxo — drena updates pendentes
    de ANTES desta execução do pipeline (ex: um clique perdido de uma rodada anterior)
    sem processá-los, só pra não confundir a revisão de agora com lixo de outra vez."""
    global _offset_updates, _offset_inicializado
    if _offset_inicializado:
        return
    _offset_inicializado = True
    try:
        resp = _requisitar_com_retry('get', f"{_API_BASE}/getUpdates",
                                      params={'timeout': 0}, timeout=15)
        updates = resp.json().get('result', [])
        if updates:
            _offset_updates = updates[-1]['update_id'] + 1
    except Exception as e:
        print(f"    ⚠️ Não consegui limpar updates antigos do Telegram ({e}) — seguindo mesmo assim")


def _proximos_updates(timeout_long_poll=25):
    """Busca updates novos do Telegram e devolve a lista crua — usado só por
    escolher_tema_telegram/escolher_destaques_telegram, que têm laços próprios (não
    passam pelas filas _fila_callbacks/_fila_mensagens porque rodam ANTES/fora do
    laço de revisão de clipe a clipe, sem risco de mistura entre clipes).

    BUGFIX: antes, um erro aqui (ex: 409 Conflict — ver _requisitar_com_retry) subia
    sem tratamento e travava a interação pro resto do workflow. Agora tenta de novo
    algumas vezes; se mesmo assim falhar, devolve lista vazia (o loop de quem chamou
    simplesmente tenta de novo no próximo ciclo) em vez de derrubar o processo inteiro.
    """
    global _offset_updates
    _descartar_atualizacoes_antigas()
    params = {'timeout': timeout_long_poll}
    if _offset_updates is not None:
        params['offset'] = _offset_updates
    try:
        resp = _requisitar_com_retry('get', f"{_API_BASE}/getUpdates", params=params,
                                      timeout=timeout_long_poll + 10)
    except Exception as e:
        print(f"    ⚠️ getUpdates do Telegram falhou repetidamente ({e}) — tentando de novo...")
        return []
    dados = resp.json()
    if not dados.get('ok'):
        return []
    updates = dados['result']
    if updates:
        _offset_updates = updates[-1]['update_id'] + 1
    return updates


def _classificar_update(upd):
    """Devolve ('callback', data) ou ('mensagem', dict) ou None — NUNCA descarta um
    update reconhecível, só ignora update de outro chat/tipo que não interessa (ex:
    edited_message, my_chat_member)."""
    cq = upd.get('callback_query')
    if cq and str(cq.get('message', {}).get('chat', {}).get('id')) == str(TELEGRAM_CHAT_ID):
        try:
            _chamar('answerCallbackQuery', callback_query_id=cq['id'])
        except Exception:
            pass
        return ('callback', cq.get('data'))

    msg = upd.get('message')
    if not msg or str(msg.get('chat', {}).get('id')) != str(TELEGRAM_CHAT_ID):
        return None

    if msg.get('photo'):
        maior = max(msg['photo'], key=lambda p: p.get('file_size', 0))
        caminho = _baixar_arquivo_telegram(maior['file_id'], _pasta_downloads_padrao(), '.jpg')
        return ('mensagem', {'tipo': 'foto', 'caminho': caminho, 'texto': None})
    if msg.get('video'):
        caminho = _baixar_arquivo_telegram(msg['video']['file_id'], _pasta_downloads_padrao(), '.mp4')
        return ('mensagem', {'tipo': 'video', 'caminho': caminho, 'texto': None})
    if msg.get('document'):
        nome = msg['document'].get('file_name', 'arquivo')
        ext = os.path.splitext(nome)[1].lower() or '.bin'
        caminho = _baixar_arquivo_telegram(msg['document']['file_id'], _pasta_downloads_padrao(), ext)
        return ('mensagem', {'tipo': 'documento', 'caminho': caminho, 'texto': None})
    if msg.get('text'):
        return ('mensagem', {'tipo': 'texto', 'caminho': None, 'texto': msg['text'].strip()})
    return None


def _drenar_para_filas(timeout_long_poll=25):
    """Busca updates novos e empilha CADA UM na fila certa (_fila_callbacks ou
    _fila_mensagens) — a peça central do bugfix: nada é descartado só porque quem
    chamou não é quem esperava por aquele tipo específico de resposta."""
    for upd in _proximos_updates(timeout_long_poll=timeout_long_poll):
        classificado = _classificar_update(upd)
        if not classificado:
            continue
        tipo, valor = classificado
        (_fila_callbacks if tipo == 'callback' else _fila_mensagens).append(valor)


def aguardar_callback(timeout_s=1800):
    """Espera o usuário apertar um botão inline. Olha a fila ANTES de fazer
    long-polling — se o clique já tinha chegado (ex: enquanto processava o clipe
    anterior), pega ele na hora em vez de esperar de novo. Retorna None no timeout."""
    limite = time.time() + timeout_s
    while time.time() < limite:
        if _fila_callbacks:
            return _fila_callbacks.pop(0)
        _drenar_para_filas(timeout_long_poll=25)
    return None


def aguardar_midia_ou_texto(timeout_s=1800, download_dir=None):
    """Espera a PRÓXIMA mensagem (não-callback): foto, vídeo, documento ou texto.
    Mesma lógica de fila-primeiro de aguardar_callback. Retorna None no timeout."""
    limite = time.time() + timeout_s
    while time.time() < limite:
        if _fila_mensagens:
            return _fila_mensagens.pop(0)
        _drenar_para_filas(timeout_long_poll=25)
    return None


def _aguardar_callback_ou_midia(timeout_s=1800):
    """Espera OU um clique de botão OU uma mídia/texto direto — o que chegar primeiro.
    É isso que permite responder um clipe SEM precisar clicar Recusar→Enviar mídia:
    manda a foto/link direto que já vale como substituição. Retorna (None, None) no
    timeout, ('callback', data) ou ('mensagem', dict)."""
    limite = time.time() + timeout_s
    while time.time() < limite:
        if _fila_callbacks:
            return ('callback', _fila_callbacks.pop(0))
        if _fila_mensagens:
            return ('mensagem', _fila_mensagens.pop(0))
        _drenar_para_filas(timeout_long_poll=25)
    return (None, None)


def _baixar_arquivo_telegram(file_id, download_dir, extensao):
    info = _chamar('getFile', file_id=file_id)
    file_path = info['file_path']
    url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
    destino = os.path.join(download_dir, f"tg_{file_id[:16]}{extensao}")
    resp = com_watchdog(requests.get, url, timeout=60,
                         timeout_total=90, label="download de arquivo do Telegram")
    if resp is None or not resp.ok:
        raise RuntimeError(f"Falha ao baixar arquivo do Telegram (file_id={file_id})")
    with open(destino, 'wb') as f:
        f.write(resp.content)
    if extensao.lower() in ('.jpg', '.jpeg', '.png'):
        _normalizar_imagem(destino)
    return destino


def _normalizar_imagem(caminho, largura_max=1920):
    """
    BUGFIX (renderização travando em ~4%, sempre no mesmo frame): foto mandada direto
    do celular pelo Telegram costuma vir em 3000-4000px+ de largura — bem maior que
    qualquer imagem que o pipeline automático já lida (Pexels/Wikimedia já vêm em
    resolução moderada). O efeito de zoom (_clip_de_imagem_com_zoom, em
    generate_video.py) reprocessa a imagem A CADA FRAME pra animar o zoom — com uma
    foto de celular gigante sem redimensionar antes, cada frame fica MUITO mais caro
    de calcular, e como o zoom dura o clipe inteiro, a exportação não trava de vez,
    só fica absurdamente lenta (segundos por frame em vez de frames por segundo) —
    o que parece travado mas é só um vídeo de ~30-60s de duração real do clipe
    levando dezenas de minutos pra renderizar.

    Redimensiona pra no máximo 'largura_max' no lado maior (de sobra pra qualquer
    resolução de saída do vídeo, sem carregar peso à toa) e corrige a rotação EXIF
    (foto de celular quase sempre tem isso, e sem corrigir alguns leitores mostram a
    imagem de lado). Roda uma vez, no download — nunca durante a renderização.
    """
    try:
        img = Image.open(caminho)
        img = ImageOps.exif_transpose(img)  # corrige rotação de foto de celular
        if img.mode not in ('RGB',):
            img = img.convert('RGB')
        if max(img.size) > largura_max:
            escala = largura_max / max(img.size)
            novo_tamanho = (max(1, int(img.width * escala)), max(1, int(img.height * escala)))
            img = img.resize(novo_tamanho, Image.LANCZOS)
            print(f"    🖼️ Imagem redimensionada pra {novo_tamanho[0]}x{novo_tamanho[1]} "
                  f"antes de entrar no pipeline (estava maior que {largura_max}px)")
        img.save(caminho, quality=90)
    except Exception as e:
        print(f"    ⚠️ Falha ao normalizar imagem '{caminho}' ({e}) — usando como veio, "
              f"pode deixar a renderização mais lenta se for muito grande")


# ============================================================
# Substituição de mídia via link do Pexels
# ============================================================

def _extrair_id_pexels(url):
    """Extrai o ID numérico do fim de uma URL de vídeo/foto do Pexels
    (ex: .../video/aerial-city-1234567/ -> '1234567')."""
    m = re.search(r'-(\d+)/?(?:$|[?#])', url.strip())
    return m.group(1) if m else None


def _baixar_pexels_por_id(url, download_dir=None, largura_alvo=1920):
    """Aceita link de /video/ ou /photo/ do Pexels e baixa a variante daquele item
    específico mais próxima de 'largura_alvo' (não uma busca — o ID exato que a URL
    aponta). Retorna o caminho local ou None se não conseguir resolver (URL sem ID,
    sem chave de API, ou erro de rede — sempre logado, nunca deixa a revisão travada
    sem explicação).

    BUGFIX (vídeo suspeito de travar a renderização em ~4%): antes pegava sempre a
    MAIOR resolução disponível (podia vir 4K de um vídeo Pexels) — bem mais pesado pra
    decodificar/redimensionar que o necessário, já que o vídeo final não passa da
    resolução configurada mesmo. O resto do pipeline (baixar_clipes_pexels,
    _escolher_arquivo_video) já escolhe a variante mais PRÓXIMA da largura alvo, não a
    maior — agora aqui faz o mesmo, por consistência e performance."""
    download_dir = download_dir or os.path.join('assets', 'telegram_review')
    os.makedirs(download_dir, exist_ok=True)

    pexels_id = _extrair_id_pexels(url)
    if not pexels_id:
        print(f"    ⚠️ Não achei um ID de item do Pexels nesse link: '{url}'")
        return None
    if not PEXELS_API_KEY:
        print("    ⚠️ PEXELS_API_KEY não configurada — não dá pra baixar por link do Pexels")
        return None

    headers = {"Authorization": PEXELS_API_KEY}
    eh_foto = '/photo/' in url or '/foto/' in url
    try:
        if eh_foto:
            resp = requests.get(f"https://api.pexels.com/v1/photos/{pexels_id}",
                                 headers=headers, timeout=20)
            resp.raise_for_status()
            src = resp.json().get('src', {})
            link = src.get('large2x') or src.get('original')
            destino = os.path.join(download_dir, f"pexels_photo_{pexels_id}.jpg")
        else:
            resp = requests.get(f"https://api.pexels.com/videos/videos/{pexels_id}",
                                 headers=headers, timeout=20)
            resp.raise_for_status()
            arquivos = [vf for vf in resp.json().get('video_files', []) if vf.get('link') and vf.get('width')]
            link = min(arquivos, key=lambda vf: abs(vf['width'] - largura_alvo))['link'] if arquivos else None
            destino = os.path.join(download_dir, f"pexels_video_{pexels_id}.mp4")

        if not link:
            print(f"    ⚠️ Item {pexels_id} do Pexels não tem arquivo baixável")
            return None

        conteudo = com_watchdog(requests.get, link, timeout=60,
                                 timeout_total=90, label=f"download Pexels {pexels_id}")
        if conteudo is None or not conteudo.ok:
            return None
        with open(destino, 'wb') as f:
            f.write(conteudo.content)
        if eh_foto:
            _normalizar_imagem(destino, largura_max=largura_alvo)
        return destino
    except Exception as e:
        print(f"    ⚠️ Falha ao baixar item {pexels_id} do Pexels ({e})")
        return None


# ============================================================
# Trecho de roteiro correspondente a uma janela de tempo
# ============================================================

def _trecho_do_roteiro(texto_segmento, palavras_tempo, inicio, fim):
    """Reconstrói o trecho do roteiro ORIGINAL (nunca o texto que o Whisper
    reconheceu — só o TEMPO dele é usado) narrado dentro de [inicio, fim), mesmo
    pareamento posicional usado em mapear_tempos_para_blocos/gerar_clips_legenda."""
    palavras = texto_segmento.split()
    n = min(len(palavras), len(palavras_tempo))
    indices = [i for i in range(n)
               if palavras_tempo[i]['fim'] > inicio and palavras_tempo[i]['inicio'] < fim]
    if not indices:
        return "(sobra de tempo sem palavra mapeada — provavelmente o fim de um bloco)"
    return " ".join(palavras[indices[0]:indices[-1] + 1])


# ============================================================
# Revisão de mídia, clipe a clipe
# ============================================================

def revisar_midia_pipeline(lista_clipes, texto_segmento, palavras_tempo, nome_segmento,
                            largura_alvo=1920):
    """
    Chamada de dentro de renderizar_segmento_webdoc, DEPOIS de baixar_clipes_por_bloco
    e ANTES de montar o vídeo (_montar_clips_pexels) — lista_clipes ainda está em
    formato bruto [{'path','inicio','duracao',...}], tempo relativo ao início da
    narração do segmento (mesmo referencial de texto_segmento/palavras_tempo).

    Cada clipe pode ser respondido de DOIS jeitos, sem precisar escolher um só:
      - clicando ✅ Aprovar / ❌ Recusar
      - mandando a substituição DIRETO (link do Pexels ou foto/vídeo do aparelho),
        sem precisar clicar em nada antes — vale como "recusar + já aqui está a mídia"
    Isso deixa mandar tudo em sequência rápida (como a maioria das pessoas naturalmente
    faz) sem precisar esperar o bot reperguntar a cada clipe.

    largura_alvo: passado pra _baixar_pexels_por_id — garante que um link de Pexels
    baixe a variante de resolução mais próxima do vídeo final (não a maior disponível,
    que pode ser 4K e pesar bem mais na hora de renderizar sem ganho nenhum de
    qualidade perceptível no resultado final).

    Devolve a MESMA lista, com 'path' trocado nos clipes que foram substituídos.
    Levanta WorkflowCanceladoPeloUsuario se o usuário cancelar.
    """
    if not ATIVA_TELEGRAM or not lista_clipes:
        return lista_clipes

    timeout_min = _timeout_min('telegram_review', 30)
    # BUGFIX: nunca herdar uma mensagem/callback que sobrou sem uso de um segmento ou
    # clipe anterior — impede vazamento tipo "mídia mandada pra um clipe da introdução
    # aparecendo no capítulo 1".
    limpar_filas_pendentes()

    print(f"  📲 Revisão de mídia via Telegram — segmento '{nome_segmento}' "
          f"({len(lista_clipes)} clipe(s), timeout {timeout_min} min/resposta)...")

    enviar_texto(f"🎬 Revisão de mídia — segmento \"{nome_segmento}\"\n"
                 f"{len(lista_clipes)} clipe(s) pra aprovar, um de cada vez. Pode "
                 f"aprovar/recusar pelos botões OU já mandar a substituição direto "
                 f"(link do Pexels ou foto/vídeo do aparelho) que eu entendo.")

    for i, clipe in enumerate(lista_clipes):
        trecho = _trecho_do_roteiro(texto_segmento, palavras_tempo,
                                     clipe['inicio'], clipe['inicio'] + clipe['duracao'])
        legenda = (f"Clipe {i + 1}/{len(lista_clipes)} — {clipe['duracao']:.1f}s "
                   f"(fonte: {clipe.get('fonte', 'pexels')})\n\n\"{trecho}\"")

        enviar_midia(clipe['path'], legenda,
                     botoes=[('✅ Aprovar', 'aprovar'), ('❌ Recusar', 'recusar')])

        tipo, valor = _aguardar_callback_ou_midia(timeout_s=timeout_min * 60)

        if tipo is None:
            print(f"    ⏱️ Sem resposta em {timeout_min} min pro clipe {i + 1} — "
                  f"aprovando automaticamente pra não travar o pipeline")
            continue

        if tipo == 'callback' and valor == 'aprovar':
            continue

        novo_caminho = None

        if tipo == 'mensagem':
            # usuário já mandou a substituição direto, sem clicar em nada
            novo_caminho = (_baixar_pexels_por_id(valor['texto'], largura_alvo=largura_alvo)
                             if valor['tipo'] == 'texto' else valor['caminho'])
            if not novo_caminho:
                enviar_texto(f"⚠️ Não consegui usar essa mídia pro clipe {i + 1} — "
                             f"mantendo o clipe original.")

        else:  # tipo == 'callback' e valor == 'recusar' → fluxo explícito de botões
            while novo_caminho is None:
                enviar_texto("O que fazer com esse clipe?",
                             botoes=[('🚫 Cancelar workflow', 'cancelar'), ('📤 Enviar mídia', 'enviar')])
                escolha = aguardar_callback(timeout_s=timeout_min * 60)

                if escolha is None or escolha == 'cancelar':
                    enviar_texto("🚫 Workflow cancelado. Nenhum vídeo será publicado.")
                    raise WorkflowCanceladoPeloUsuario(
                        f"Cancelado pelo usuário no clipe {i + 1} do segmento '{nome_segmento}'")

                enviar_texto("Manda o link do Pexels (pexels.com/video/... ou /photo/...) "
                             "ou envie a foto/vídeo direto daqui.")
                recebido = aguardar_midia_ou_texto(timeout_s=timeout_min * 60)

                if recebido is None:
                    enviar_texto(f"⏱️ Sem resposta em {timeout_min} min — mantendo o clipe original.")
                    break

                novo_caminho = (_baixar_pexels_por_id(recebido['texto'], largura_alvo=largura_alvo)
                                 if recebido['tipo'] == 'texto' else recebido['caminho'])
                if not novo_caminho:
                    enviar_texto("⚠️ Não consegui usar essa mídia — manda de novo, ou cancela.")

        if novo_caminho:
            clipe['path'] = novo_caminho
            clipe['fonte'] = 'telegram_manual'
            enviar_texto(f"✅ Clipe {i + 1} substituído, seguindo pro próximo.")

    enviar_texto(f"✅ Revisão do segmento \"{nome_segmento}\" concluída.")
    return lista_clipes


# ============================================================
# Seleção de tema no início do workflow
# ============================================================

# ============================================================
# Escolha manual de palavras de destaque
# ============================================================

def escolher_destaques_telegram(texto_segmento, nome_segmento, timeout_min=None):
    """
    Manda o texto INTEIRO do segmento e pergunta quais expressões destacar, em vez de
    deixar o Gemini escolher sozinho (escolher_palavras_destaque, em producao_visual.py).
    Resposta esperada: uma expressão por linha, EXATAMENTE como aparece no texto
    mandado (mapear_destaques_manuais_para_blocos, em producao_visual.py, faz a
    checagem literal depois). Botão "Automático" ou timeout → retorna None, e quem
    chama cai pra escolher_palavras_destaque() de sempre.
    """
    if not ATIVA_TELEGRAM:
        return None
    if timeout_min is None:
        timeout_min = _timeout_min('destaques_telegram', 15)

    texto_exibicao = texto_segmento if len(texto_segmento) <= 3500 else texto_segmento[:3500] + " (…)"
    enviar_texto(
        f"✨ Segmento \"{nome_segmento}\" — quais palavras/expressões quer destacar?\n\n"
        f"{texto_exibicao}\n\n"
        f"Responda com uma expressão POR LINHA, EXATAMENTE como está escrita acima "
        f"(1 a 3 palavras cada, é assim que vão aparecer na tela). Ou aperta o botão "
        f"pra deixar o Gemini escolher automático.",
        botoes=[('🤖 Automático', 'automatico')]
    )

    limite = time.time() + timeout_min * 60
    while time.time() < limite:
        for upd in _proximos_updates(timeout_long_poll=25):
            cq = upd.get('callback_query')
            if cq and str(cq.get('message', {}).get('chat', {}).get('id')) == str(TELEGRAM_CHAT_ID):
                try:
                    _chamar('answerCallbackQuery', callback_query_id=cq['id'])
                except Exception:
                    pass
                if cq.get('data') == 'automatico':
                    print("  🤖 Usuário escolheu destaque automático")
                    return None
            msg = upd.get('message')
            if msg and str(msg.get('chat', {}).get('id')) == str(TELEGRAM_CHAT_ID) and msg.get('text'):
                frases = [l.strip() for l in msg['text'].splitlines() if l.strip()]
                if frases:
                    enviar_texto(f"✨ {len(frases)} expressão(ões) marcada(s) pra destaque.")
                    print(f"  ✨ Destaques manuais recebidos via Telegram: {frases}")
                    return frases

    print(f"  ⏱️ Sem resposta de destaques em {timeout_min} min — automático")
    return None


def escolher_tema_telegram(timeout_min=None):
    """
    Pergunta o tema/direcionamento do próximo vídeo, com botão "Nada a sugerir". Texto
    de resposta vira o tema (não precisa ser um título pronto — pode ser só o
    direcionamento, ex: "Braskem, o estrago que fez em Maceió com a extração de
    salgema"). Botão OU timeout → retorna None, e quem chama cai pra escolha automática
    (escolher_tema_reflexao(), dentro de generate_video.py).
    """
    if not ATIVA_TELEGRAM:
        return None
    if timeout_min is None:
        timeout_min = _timeout_min('selecao_tema_telegram', 10)

    enviar_texto(
        "🎯 Qual o tema do próximo vídeo? Pode mandar só o direcionamento, não precisa "
        "ser o título pronto (ex: \"a morosidade da transposição do Rio São Francisco\"). "
        "Ou aperta o botão se quiser que eu escolha.",
        botoes=[('🤖 Nada a sugerir', 'nada_a_sugerir')]
    )

    limite = time.time() + timeout_min * 60
    while time.time() < limite:
        for upd in _proximos_updates(timeout_long_poll=25):
            cq = upd.get('callback_query')
            if cq and str(cq.get('message', {}).get('chat', {}).get('id')) == str(TELEGRAM_CHAT_ID):
                try:
                    _chamar('answerCallbackQuery', callback_query_id=cq['id'])
                except Exception:
                    pass
                if cq.get('data') == 'nada_a_sugerir':
                    print("  🤖 Usuário escolheu 'Nada a sugerir' — tema automático")
                    return None
            msg = upd.get('message')
            if msg and str(msg.get('chat', {}).get('id')) == str(TELEGRAM_CHAT_ID) and msg.get('text'):
                tema = msg['text'].strip()
                enviar_texto(f"👍 Tema recebido: \"{tema}\". Gerando o roteiro...")
                print(f"  🎯 Tema recebido via Telegram: \"{tema}\"")
                return tema

    print(f"  ⏱️ Sem resposta de tema em {timeout_min} min — escolhendo automaticamente")
    return None
