import os
import json
import random
import re
import asyncio
import time
from datetime import datetime
import requests
import edge_tts
from PIL import Image, ImageDraw, ImageFont
from moviepy.editor import *
from google import generativeai as genai
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from roteiro_engine import gerar_pacote_roteiro
from producao_visual import (
    mapear_tempos_para_blocos,
    escolher_termos_por_bloco,
    escolher_palavras_destaque,
    resolver_destaques_com_tempo,
    construir_timeline_sfx,
    decidir_prints_de_noticia,
)
from transicoes import TRANSICOES_DISPONIVEIS, transicao_crossfade
from mockups_visuais import gerar_print_noticia

# ============================================================
# Curadoria via Telegram (opcional)
# ============================================================
try:
    from telegram_curator_noticias import TelegramCuratorNoticias
    CURACAO_DISPONIVEL = True
except ImportError:
    print("⚠️ telegram_curator_noticias.py não encontrado")
    CURACAO_DISPONIVEL = False

CONFIG_FILE = 'config.json'
VIDEOS_DIR = 'videos'
ASSETS_DIR = 'assets'
VIDEO_TYPE = os.environ.get('VIDEO_TYPE', 'short')  # 'short' (vertical) ou 'long' (horizontal)

GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
YOUTUBE_CLIENT_ID = os.environ.get('YOUTUBE_CLIENT_ID')
YOUTUBE_CLIENT_SECRET = os.environ.get('YOUTUBE_CLIENT_SECRET')
YOUTUBE_REFRESH_TOKEN = os.environ.get('YOUTUBE_REFRESH_TOKEN')
PEXELS_API_KEY = os.environ.get('PEXELS_API_KEY')

# ── Agnes AI (geração de imagem gratuita, para thumbnail) ───────────────────
# ⚠️ Serviço de terceiro relativamente novo (2026) — sem o histórico de estabilidade do
# Pexels. Por isso é usado como PRIMEIRA opção com fallback automático pro Pexels, não como
# única fonte.
AGNES_API_KEY = os.environ.get('AGNES_API_KEY')
AGNES_IMAGE_MODEL = os.environ.get('AGNES_IMAGE_MODEL', 'agnes-image-2.1-flash')
AGNES_URL = "https://apihub.agnes-ai.com/v1/images/generations"
AGNES_VIDEO_MODEL = os.environ.get('AGNES_VIDEO_MODEL', 'agnes-video-v2.0')
AGNES_VIDEO_URL = "https://apihub.agnes-ai.com/v1/videos"

# ── Fish Audio (voz) ─────────────────────────────────────────────────────────
FISHAUDIO_API_KEY = os.environ.get('FISHAUDIO_API_KEY')
FISHAUDIO_VOICE_ID = os.environ.get('FISHAUDIO_VOICE_ID')
FISHAUDIO_MODEL = os.environ.get('FISHAUDIO_MODEL', 's2.1-pro-free')
FISHAUDIO_URL = "https://api.fish.audio/v1/tts"

GEMINI_TEXT_MODEL = os.environ.get('GEMINI_TEXT_MODEL', 'gemini-3.5-flash-lite')

USAR_CURACAO = os.environ.get('USAR_CURACAO', 'false').lower() == 'true' and CURACAO_DISPONIVEL
CURACAO_TIMEOUT = int(os.environ.get('CURACAO_TIMEOUT', '3600'))

# ── Modo de teste rápido (não usar em produção) ──────────────────────────────
LIMITE_CLIPES_TESTE = int(os.environ.get('LIMITE_CLIPES_TESTE', '0'))  # 0 = sem limite
PULAR_UPLOAD = os.environ.get('PULAR_UPLOAD', 'false').lower() == 'true'

# ── Estrutura de tempo do vídeo ──────────────────────────────────────────────
SEGUNDOS_LEAD_IN = float(os.environ.get('SEGUNDOS_LEAD_IN', '3'))   # vídeo+música antes da narração
SEGUNDOS_TAIL = float(os.environ.get('SEGUNDOS_TAIL', '5'))         # vídeo+música depois da narração
SEGUNDOS_FADEOUT = float(os.environ.get('SEGUNDOS_FADEOUT', '2'))   # fade-out no final (vídeo + áudio)

# ── Duração máxima por clipe do Pexels ───────────────────────────────────────
# Evita que um único vídeo longo (ex: 2min) preencha o short inteiro sozinho.
# Ajuste entre 15 e 30 conforme preferir mais ou menos variedade de cortes.
DURACAO_MAXIMA_CLIPE = float(os.environ.get('DURACAO_MAXIMA_CLIPE', '14'))  # dinamismo: nada fica mais que isso na tela
# Teto de quanto tempo um print de notícia fica sozinho na tela — o resto do bloco
# (se a narração daquele trecho for mais longa que isso) cai pro B-roll normal.


# ── Legenda automática ───────────────────────────────────────────────────────
ATIVAR_LEGENDA = os.environ.get('ATIVAR_LEGENDA', 'true').lower() == 'true'
ATIVAR_DESTAQUE = os.environ.get('ATIVAR_DESTAQUE', 'true').lower() == 'true'  # Fase 2: texto de destaque
ATIVAR_SFX = os.environ.get('ATIVAR_SFX', 'true').lower() == 'true'  # Fase 2: SFX de transição/destaque
MAX_PAGINAS_BLOCO = int(os.environ.get('MAX_PAGINAS_BLOCO', '3'))  # páginas Pexels por bloco (não por vídeo)
# LEGENDA_FONTE é definida logo após o carregamento do config.json (precisa do config.get(...))

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(GEMINI_TEXT_MODEL)


def _gemini_generate(prompt, tentativas=3, espera=15):
    """
    Chama o Gemini com retry/backoff. Sem isso, qualquer instabilidade transitória da API
    (ex: 504 DeadlineExceeded, 503 ServiceUnavailable, 429 rate limit) derruba o workflow
    inteiro sem necessidade — geralmente uma segunda tentativa alguns segundos depois resolve.
    """
    ultimo_erro = None
    for tentativa in range(1, tentativas + 1):
        try:
            return model.generate_content(prompt)
        except Exception as e:
            ultimo_erro = e
            print(f"  ⚠️ Erro no Gemini (tentativa {tentativa}/{tentativas}): {e}")
            if tentativa < tentativas:
                time.sleep(espera * tentativa)  # backoff progressivo: 15s, 30s, ...
    raise ultimo_erro

with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
    config = json.load(f)

DURACAO_MAXIMA_PRINT_NOTICIA = float(config.get('duracao_maxima_print_noticia', 9))
# Idioma do conteúdo gerado (roteiro, título, thumbnail) — mude só isso no config.json
# pra clonar o canal em outro idioma, sem tocar no código.
IDIOMA_CONTEUDO = config.get('idioma_conteudo', 'português do Brasil')
CONTEXTO_NICHO = config.get('contexto_nicho', 'reflexão cristã/motivacional')
INSTRUCAO_EXTRA_ROTEIRO = config.get('instrucao_extra_roteiro', '')

# Fonte da legenda: nome da família (como o ImageMagick/fontconfig reconhece após instalado
# no runner — veja o step do .yml que instala as fontes de fonts/ no sistema).
# Liberation Sans Bold é o padrão livre, geralmente já vem instalado no runner sem esforço extra.
LEGENDA_FONTE = os.environ.get('LEGENDA_FONTE', config.get('fonte_legenda_nome', 'Liberation-Sans-Bold'))
COR_DESTAQUE = os.environ.get('COR_DESTAQUE', config.get('cor_destaque', '#FFD24D'))  # Fase 2: texto de destaque
FONTE_DESTAQUE = os.environ.get('FONTE_DESTAQUE', config.get('fonte_destaque_arquivo', config.get('fonte_destaque_nome', LEGENDA_FONTE)))
DURACAO_POP_DESTAQUE = float(os.environ.get('DURACAO_POP_DESTAQUE', '0.18'))  # tempo do "estalo" de entrada

# Tamanho da palavra-destaque: por padrão é largura_do_video / DESTAQUE_TAMANHO_DIVISOR
# (divisor maior = texto menor). Ajustável em config.json ('fonte_destaque_divisor') sem
# mexer em código — ex: 6 deixa bem maior que o padrão (10), 14 deixa bem menor.
# (Baixamos o padrão de 8 pra 10: em largura/8 o texto ficava grande demais em 1080px de
# largura — 135px de fonte — forçando quebra feia mesmo em palavras curtas.)
DESTAQUE_TAMANHO_DIVISOR = float(os.environ.get(
    'DESTAQUE_TAMANHO_DIVISOR', config.get('fonte_destaque_divisor', 10)
))
# Override direto em pixels, se preferir controle absoluto em vez de proporcional à
# largura do vídeo — deixe null/ausente em config.json pra usar o divisor acima.
DESTAQUE_TAMANHO_PX = config.get('fonte_destaque_tamanho_px')

# Fonte da thumbnail: caminho direto do arquivo .ttf no repositório (PIL carrega o arquivo
# diretamente, não precisa estar instalada no sistema).
FONTE_THUMBNAIL_ARQUIVO = config.get('fonte_thumbnail_arquivo', '')


# ============================================================
# TEMA DO DIA — rotação sem repetição
# ============================================================

TEMAS_LOG_FILE = 'temas_usados.json'


def _carregar_temas_usados():
    if os.path.exists(TEMAS_LOG_FILE):
        try:
            with open(TEMAS_LOG_FILE, 'r', encoding='utf-8') as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def _salvar_tema_usado(tema):
    usados = _carregar_temas_usados()
    usados.add(tema)
    with open(TEMAS_LOG_FILE, 'w', encoding='utf-8') as f:
        json.dump(sorted(usados), f, indent=2, ensure_ascii=False)


def escolher_tema_reflexao():
    temas_config = config.get('temas_reflexao', [])
    usados = _carregar_temas_usados()
    disponiveis = [t for t in temas_config if t not in usados]

    if disponiveis:
        tema = random.choice(disponiveis)
        print(f"💭 Tema (da lista configurada): {tema}")
        return tema

    print("💭 Lista de temas esgotada — pedindo sugestão ao Gemini...")
    prompt = f"""Sugira UM tema de {CONTEXTO_NICHO} (ex: gratidão, perdão, esperança, disciplina,
superação), que NÃO esteja nesta lista já usada: {sorted(usados)}

Responda em {IDIOMA_CONTEUDO}, APENAS com o nome do tema, curto."""
    resposta = _gemini_generate(prompt)
    tema = resposta.text.strip().strip('"')
    print(f"💭 Tema (sugerido pelo Gemini): {tema}")
    return tema


def gerar_titulo(tema):
    prompt = f"""Baseado no tema de {CONTEXTO_NICHO} "{tema}", crie um título de vídeo curto e
chamativo para YouTube (estilo motivacional/inspiracional), em {IDIOMA_CONTEUDO}.

Retorne APENAS JSON: {{"titulo": "título aqui"}}"""

    response = _gemini_generate(prompt)
    texto = response.text.strip().replace('```json', '').replace('```', '').strip()
    inicio = texto.find('{')
    fim = texto.rfind('}') + 1

    if inicio == -1 or fim == 0:
        return tema
    try:
        return json.loads(texto[inicio:fim]).get('titulo', tema)
    except Exception:
        return tema


def gerar_roteiro(tema, tipo_video):
    if tipo_video == 'short':
        palavras_alvo = 180
        duracao_desc = '60-90 segundos'
    else:
        palavras_alvo = 650
        duracao_desc = '4-5 minutos'

    linha_extra = f"\n- {INSTRUCAO_EXTRA_ROTEIRO}" if INSTRUCAO_EXTRA_ROTEIRO else ""

    prompt = f"""Crie um roteiro de narração para um vídeo de {CONTEXTO_NICHO} sobre o tema:
"{tema}"

REGRAS OBRIGATÓRIAS:
- Escreva em {IDIOMA_CONTEUDO}
- Duração alvo: {duracao_desc} de narração (~{palavras_alvo} palavras)
- Tom acolhedor, reflexivo, encorajador — como uma conversa sincera, não um sermão/palestra formal
- Termine com uma mensagem de esperança/encorajamento prática para o dia a dia{linha_extra}
- NÃO mencione apresentador, elementos visuais ou câmera
- Texto corrido, pronto para narração
- SEM formatação, asteriscos, marcadores ou emojis

Escreva APENAS o roteiro."""

    response = _gemini_generate(prompt)
    texto = response.text
    texto = re.sub(r'\*+', '', texto)
    texto = re.sub(r'#+\s', '', texto)
    texto = re.sub(r'^-\s', '', texto, flags=re.MULTILINE)
    texto = texto.replace('*', '').replace('#', '').replace('_', '').strip()
    return texto


def revisar_roteiro(roteiro):
    """
    Segunda passada pelo Gemini, só pra revisão ortográfica/gramatical.
    Ajuda a pegar palavras inventadas/mal formadas que o modelo eventualmente produz
    (mais comum em conjugações verbais raras do português, ex: "formurmos" em vez de "formos").
    Não muda sentido nem tom — só corrige erros de escrita.
    Se falhar por qualquer motivo, devolve o roteiro original sem revisão (não quebra o vídeo).
    """
    prompt = f"""Revise o texto abaixo em {IDIOMA_CONTEUDO}. Corrija SOMENTE erros de ortografia,
gramática, concordância ou palavras inexistentes/mal formadas (ex: conjugações verbais erradas).
NÃO mude o sentido, o tom, nem reescreva frases que já estão corretas. Se não houver nenhum erro,
devolva o texto exatamente como está, sem alterar nada.

TEXTO:
{roteiro}

Retorne APENAS o texto revisado, sem comentários, sem aspas, sem explicações."""

    try:
        resposta = _gemini_generate(prompt)
        texto_revisado = resposta.text.strip()
        if texto_revisado:
            return texto_revisado
    except Exception as e:
        print(f"⚠️ Revisão do roteiro falhou: {e} — usando versão original sem revisão")

    return roteiro


# ============================================================
# ÁUDIO — Fish Audio (voz clonada) com fallback para Edge TTS
# ============================================================

def _chamada_crua_fishaudio(texto, output_file):
    """Uma única chamada à API da Fish Audio, sem chunking — usada internamente por trecho."""
    if not FISHAUDIO_API_KEY:
        raise Exception("FISHAUDIO_API_KEY não configurada")

    headers = {
        "Authorization": f"Bearer {FISHAUDIO_API_KEY}",
        "Content-Type": "application/json",
        "model": FISHAUDIO_MODEL  # vai no header, não no body
    }
    payload = {"text": texto, "reference_id": FISHAUDIO_VOICE_ID, "format": "mp3"}

    resp = requests.post(FISHAUDIO_URL, headers=headers, json=payload, timeout=180)
    resp.raise_for_status()

    with open(output_file, 'wb') as f:
        f.write(resp.content)

    if not os.path.exists(output_file) or os.path.getsize(output_file) == 0:
        raise Exception("Arquivo de áudio vazio retornado pela Fish Audio")


def _dividir_em_frases(texto):
    """Divide o roteiro em frases (por pontuação), pra gerar áudio em trechos menores."""
    frases = re.split(r'(?<=[.!?])\s+', texto.strip())
    return [f.strip() for f in frases if f.strip()]


def aplicar_correcoes_pronuncia(texto):
    """
    Troca palavras conhecidas por má pronúncia da voz clonada (ex: "fascinante" -> "fassinante")
    por uma grafia mais fácil pro TTS, SEM alterar a contagem de palavras — cada palavra errada
    vira exatamente uma palavra de substituição. Isso é o que garante que a legenda (que usa o
    texto original correto) continue alinhada com o áudio (que usa o texto "fonético").
    Lista curada em config.json -> "correcoes_pronuncia" (chave = palavra correta, minúscula).
    """
    correcoes = config.get('correcoes_pronuncia', {})
    if not correcoes:
        return texto

    def substituir(match):
        palavra_original = match.group(0)
        palavra_lower = palavra_original.lower()
        if palavra_lower not in correcoes:
            return palavra_original
        substituta = correcoes[palavra_lower]
        # Preserva capitalização simples (inicial maiúscula ou tudo maiúsculo)
        if palavra_original.isupper():
            return substituta.upper()
        if palavra_original[0].isupper():
            return substituta.capitalize()
        return substituta

    padrao = r'\b(' + '|'.join(re.escape(p) for p in correcoes.keys()) + r')\b'
    return re.sub(padrao, substituir, texto, flags=re.IGNORECASE)


def _estimar_duracao_esperada(texto, palavras_por_segundo=2.2):
    """Estimativa conservadora (fala mais devagar que a média) só pra servir de teto de sanidade."""
    n_palavras = len(texto.split())
    return max(n_palavras / palavras_por_segundo, 1.0)


def criar_audio_fishaudio(texto, output_file):
    """
    Gera a narração completa via Fish Audio, DIVIDINDO O ROTEIRO EM FRASES em vez de mandar
    tudo de uma vez. TTS neurais (incluindo clonagem de voz) são bem mais instáveis em textos
    longos — o modelo pode "alucinar" e inserir um trecho em outro idioma ou repetir algo que não
    estava no texto. Textos curtos (frase a frase) reduzem bastante esse risco.

    Cada frase tem a duração do áudio conferida contra uma estimativa esperada; se sair muito mais
    longa que o esperado, é sinal de alucinação e o trecho é regerado automaticamente.
    """
    frases = _dividir_em_frases(texto)
    print(f"  🔊 Gerando narração em {len(frases)} trecho(s) (Fish Audio)...")

    pasta_trechos = f'{ASSETS_DIR}/audio_trechos'
    os.makedirs(pasta_trechos, exist_ok=True)
    caminhos_trechos = []

    for i, frase in enumerate(frases):
        duracao_esperada = _estimar_duracao_esperada(frase)
        limite_max = max(duracao_esperada * 2.5, duracao_esperada + 4)  # margem generosa

        trecho_path = f'{pasta_trechos}/trecho_{i:03d}.mp3'
        duracao_real = None

        for tentativa in range(2):
            _chamada_crua_fishaudio(frase, trecho_path)
            clip_temp = AudioFileClip(trecho_path)
            duracao_real = clip_temp.duration
            clip_temp.close()

            if duracao_real <= limite_max:
                break
            print(f"  ⚠️ Trecho {i + 1}/{len(frases)} saiu com {duracao_real:.1f}s "
                  f"(esperado ~{duracao_esperada:.1f}s) — possível alucinação do TTS, "
                  f"tentando de novo...")
        else:
            print(f"  ⚠️ Trecho {i + 1} ainda suspeito após retry — mantendo mesmo assim "
                  f"(sem alternativa gratuita melhor no momento)")

        caminhos_trechos.append(trecho_path)

    print("  🔗 Concatenando trechos em áudio final...")
    clips_audio = [AudioFileClip(p) for p in caminhos_trechos]

    # BUGFIX (pausa cortando palavra no meio): a versão anterior detectava silêncio no
    # áudio JÁ CONCATENADO por amplitude (pydub) — uma consoante breve ou respiração no
    # MEIO de uma palavra podia parecer silêncio e a pausa entrava ali, cortando a
    # palavra. Aqui cada FRASE já é um arquivo separado (por causa da divisão acima) —
    # então o silêncio entra exatamente na junção entre arquivos, nunca no meio de uma
    # palavra, porque essa junção é um limite de frase de verdade, não uma suposição.
    duracao_pausa_s = config.get('duracao_pausa_frases_ms', 1000) / 1000
    if duracao_pausa_s > 0 and len(clips_audio) > 1:
        silencio = AudioClip(lambda t: 0 * t, duration=duracao_pausa_s, fps=44100)
        intercalados = []
        for i, c in enumerate(clips_audio):
            intercalados.append(c)
            if i < len(clips_audio) - 1:
                intercalados.append(silencio)
        audio_final = concatenate_audioclips(intercalados)
        print(f"  ✅ {len(clips_audio) - 1} pausa(s) entre frases de {duracao_pausa_s:.1f}s inserida(s)")
    else:
        audio_final = concatenate_audioclips(clips_audio)

    audio_final.write_audiofile(output_file, logger=None)
    for c in clips_audio:
        c.close()
    audio_final.close()

    if not os.path.exists(output_file) or os.path.getsize(output_file) == 0:
        raise Exception("Falha ao montar áudio final a partir dos trechos")


async def criar_audio_edge_async(texto, output_file):
    voz = config.get('voz_fallback', 'pt-BR-ThalitaMultilingualNeural')
    for tentativa in range(3):
        try:
            communicate = edge_tts.Communicate(texto, voz, rate="+0%", pitch="+0Hz")
            await asyncio.wait_for(communicate.save(output_file), timeout=180)
            print("✅ Edge TTS (fallback)")
            return
        except asyncio.TimeoutError:
            print(f"⏱️ Timeout {tentativa + 1}")
            if tentativa < 2:
                await asyncio.sleep(10)
        except Exception as e:
            print(f"⚠️ Erro {tentativa + 1}: {e}")
            if tentativa < 2:
                await asyncio.sleep(10)
    raise Exception("Edge TTS falhou")


def criar_audio(texto, output_file):
    print("🎙️ Criando narração (Fish Audio)...")
    try:
        criar_audio_fishaudio(texto, output_file)
        print("✅ Fish Audio")
        return output_file
    except Exception as e:
        print(f"⚠️ Fish Audio falhou: {e} — usando Edge TTS como fallback")

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(criar_audio_edge_async(texto, output_file))
        loop.close()
        if os.path.exists(output_file) and os.path.getsize(output_file) > 0:
            return output_file
    except Exception as e:
        print(f"❌ Edge TTS: {e}")
        from gtts import gTTS
        idioma_gtts = config.get('idioma_gtts', 'pt-br')
        tts = gTTS(text=texto, lang=idioma_gtts, slow=False)
        tts.save(output_file)
        print("⚠️ gTTS usado (último recurso)")

    return output_file


# ============================================================
# LEGENDA AUTOMÁTICA — Whisper só pra timing, não pra seleção de vídeo
# ============================================================

_whisper_model = None


def _carregar_whisper():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        print("🧠 Carregando modelo Whisper (base, CPU) para legendas...")
        _whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
    return _whisper_model


def transcrever_palavras_com_timestamps(audio_path):
    """
    Transcreve com timestamp POR PALAVRA — mas usamos só o TEMPO de cada palavra,
    nunca o texto que o Whisper reconheceu (que pode ter erros de reconhecimento/ortografia).
    O texto exibido na legenda vem sempre do roteiro original do Gemini.
    """
    whisper_model = _carregar_whisper()
    idioma_whisper = config.get('idioma_whisper', 'pt')
    segments, _info = whisper_model.transcribe(audio_path, language=idioma_whisper, word_timestamps=True)

    palavras_tempo = []
    for seg in segments:
        if seg.words:
            for w in seg.words:
                palavras_tempo.append({'inicio': w.start, 'fim': w.end})
    return palavras_tempo


def gerar_clips_legenda(roteiro, palavras_tempo, largura, altura, offset=0.0, palavras_por_bloco=7):
    """
    Gera os clipes de texto (legenda) centralizados, sincronizados com a narração.
    O TEXTO vem do roteiro original (sempre correto); o TEMPO vem do Whisper (alinhamento),
    já transcrito uma única vez em main() e reaproveitado aqui (e em gerar_clips_destaque,
    e no mapeamento de blocos) — evita rodar o Whisper mais de uma vez por vídeo.
    As palavras do roteiro são pareadas em ordem com os timestamps detectados pelo Whisper —
    pequena diferença na contagem de palavras é normal e não quebra o alinhamento, só desloca
    ligeiramente o fim do vídeo (raramente perceptível).
    offset: quantos segundos a narração está deslocada no vídeo final (lead-in + intro, se houver).
    Se falhar por qualquer motivo (ex: ImageMagick ausente), retorna lista vazia — não derruba o vídeo.
    """
    if not ATIVAR_LEGENDA:
        return []

    if not palavras_tempo:
        print("⚠️ Sem timestamps de palavra disponíveis — seguindo sem legenda")
        return []

    palavras_roteiro = roteiro.split()
    n = min(len(palavras_roteiro), len(palavras_tempo))
    if n == 0:
        return []

    if len(palavras_roteiro) != len(palavras_tempo):
        print(f"  ℹ️ Roteiro tem {len(palavras_roteiro)} palavras, Whisper detectou {len(palavras_tempo)} "
              f"marcações de tempo — alinhando pelas {n} em comum (diferença pequena é normal)")

    fontsize = max(24, int(largura / 24))  # menor — legenda de apoio, não protagonista
    largura_texto = int(largura * 0.85)
    margem_inferior = int(altura * 0.08)

    clips = []
    i = 0
    while i < n:
        fim_bloco = min(i + palavras_por_bloco, n)
        texto_bloco = " ".join(palavras_roteiro[i:fim_bloco])
        inicio_tempo = palavras_tempo[i]['inicio']
        fim_tempo = palavras_tempo[fim_bloco - 1]['fim']

        try:
            txt_clip = TextClip(
                texto_bloco,
                fontsize=fontsize,
                font=LEGENDA_FONTE,
                color='white',
                stroke_color='black',
                stroke_width=max(1, fontsize // 18),
                method='caption',
                size=(largura_texto, None),
                align='center'
            )
            txt_clip = txt_clip.set_position(('center', altura - margem_inferior - txt_clip.h))
            txt_clip = txt_clip.set_start(offset + inicio_tempo)
            txt_clip = txt_clip.set_duration(max(0.3, fim_tempo - inicio_tempo))
            clips.append(txt_clip)
        except Exception as e:
            print(f"⚠️ Erro ao gerar legenda de um bloco: {e} — pulando esse trecho")

        i = fim_bloco

    if not clips:
        print("⚠️ Nenhuma legenda gerada (possível problema com ImageMagick) — vídeo seguirá sem legenda")
    else:
        print(f"✅ {len(clips)} bloco(s) de legenda gerado(s), com texto do roteiro original")

    return clips


def gerar_clips_destaque(roteiro, palavras_tempo, destaques_resolvidos, largura, altura, offset=0.0):
    """
    Fase 2 — texto de destaque: diferente da legenda comum, isso é o "grito visual" tipo
    webdoc — só a palavra/expressão marcada como importante, maior, em cor de destaque,
    aparecendo exatamente no timestamp em que é falada.
    Efeito de entrada/saída (não é só fade): o texto nasce a 70% do tamanho e "estala" até
    100% em DURACAO_POP_DESTAQUE segundos (efeito pop), combinado com fade in/out — testado
    isoladamente antes de entrar aqui, renderiza sem erro mesmo com ImageMagick padrão.
    """
    if not ATIVAR_DESTAQUE or not destaques_resolvidos:
        return []

    # Tamanho controlável via config.json ('fonte_destaque_tamanho_px' ganha de tudo;
    # senão 'fonte_destaque_divisor', default 8 = largura/8, igual ao comportamento antigo).
    # Tamanho controlável via config.json ('fonte_destaque_tamanho_px' ganha de tudo;
    # senão 'fonte_destaque_divisor', default 8 = largura/8, igual ao comportamento antigo).
    fontsize_base = int(DESTAQUE_TAMANHO_PX) if DESTAQUE_TAMANHO_PX else max(50, int(largura / DESTAQUE_TAMANHO_DIVISOR))
    largura_texto = int(largura * 0.9)
    pos_y_alvo = int(altura * 0.5)  # centro da tela, não mais competindo com a legenda
    clips = []

    # Ordenado por instante de início — necessário pro fix de sobreposição abaixo, que
    # precisa saber quando o PRÓXIMO destaque começa pra evitar dois textos na tela
    # ao mesmo tempo, na mesma posição central.
    destaques_ordenados = sorted(destaques_resolvidos, key=lambda d: d['inicio'])

    def _fonte_pil_destaque(tamanho):
        """Mesma lógica de fallback do _carregar_fonte_pil (thumbnail), mas pra
        FONTE_DESTAQUE especificamente. Se FONTE_DESTAQUE for um caminho de arquivo
        válido, usa ele; senão cai pras fontes de sistema conhecidas."""
        if FONTE_DESTAQUE and os.path.exists(FONTE_DESTAQUE):
            return ImageFont.truetype(FONTE_DESTAQUE, tamanho)
        for caminho in _FONTE_TTF_CANDIDATOS:
            if os.path.exists(caminho):
                return ImageFont.truetype(caminho, tamanho)
        return ImageFont.load_default()

    _medidor_img = Image.new('RGB', (1, 1))
    _medidor_draw = ImageDraw.Draw(_medidor_img)

    def _largura_renderizada(texto, tamanho):
        """BUGFIX (fonte gigante quebrando palavra no meio, ex: 'Resiliênci'/'a'):
        a versão anterior media a largura chamando o ImageMagick (TextClip method='label')
        — se isso falhar silenciosamente (fonte não encontrada pelo IM, erro de processo,
        etc.), o except:pass deixava passar o fontsize_base gigante sem encolher nada.
        Medir com PIL direto é mais confiável (mesma lib já usada na thumbnail, sem
        depender de um subprocess do ImageMagick) e nunca lança silenciosamente — se a
        fonte não existir, cai pro fallback de sistema em vez de quebrar a medição.
        Aplica 12% de margem de segurança pra cobrir qualquer diferença fina de métrica
        entre o PIL (medição) e o ImageMagick (render final do 'caption')."""
        fonte = _fonte_pil_destaque(tamanho)
        bbox = _medidor_draw.textbbox((0, 0), texto, font=fonte)
        return int((bbox[2] - bbox[0]) * 1.12)

    for idx, destaque in enumerate(destaques_ordenados):
        try:
            texto_upper = destaque['texto'].upper()

            # Ajusta o fontsize pra caber: a causa da quebra no meio da palavra (ex:
            # "Resiliênci" numa linha, "a" na outra) é o fontsize fixo não considerar o
            # tamanho da palavra — em method='caption', se UMA palavra sozinha já é mais
            # larga que a caixa de texto, o ImageMagick quebra a palavra, não só a linha.
            # Medimos a palavra mais longa do destaque (não a frase toda, que pode
            # legitimamente quebrar em várias palavras) e encolhemos o fontsize até ela
            # caber inteira numa linha — em LOOP (não só uma estimativa de uma tentativa
            # só), porque a relação largura/fontsize não é perfeitamente linear entre
            # fontes/glifos diferentes, e uma única estimativa pode ainda estourar.
            fontsize = fontsize_base
            palavras = texto_upper.split()
            palavra_mais_longa = max(palavras, key=len) if palavras else texto_upper
            for _tentativa in range(6):
                largura_palavra = _largura_renderizada(palavra_mais_longa, fontsize)
                if largura_palavra <= largura_texto or fontsize <= 28:
                    break
                fontsize = max(28, int(fontsize * largura_texto / largura_palavra))

            txt_clip = TextClip(
                texto_upper,
                fontsize=fontsize,
                font=FONTE_DESTAQUE,
                color=COR_DESTAQUE,
                stroke_color='black',
                stroke_width=max(2, fontsize // 14),
                method='caption',
                size=(largura_texto, None),
                align='center'
            )
            w0, h0 = txt_clip.size

            def _escala(t, w0=w0, h0=h0):
                fator = 0.7 + 0.3 * min(t / DURACAO_POP_DESTAQUE, 1.0)
                return (max(1, int(w0 * fator)), max(1, int(h0 * fator)))

            def _posicao(t, pos_y_alvo=pos_y_alvo, escala=_escala):
                _, h_atual = escala(t)
                return ('center', pos_y_alvo - h_atual // 2)

            # duração mínima generosa: o pop (0.18s) + fade-out (0.2s) já consomem ~0.4s,
            # então precisa sobrar tempo de fato "parado" e legível na tela — antes só tinha
            # 0.6s de mínimo, quase tudo consumido pela própria transição de entrada/saída
            duracao = max(1.3, (destaque['fim'] - destaque['inicio']) + 0.7)

            # BUGFIX (destaque sobrepondo o outro, ex: "tribulação" ainda na tela quando
            # "perseverança" começa): as duas palavras são desenhadas na MESMA posição
            # central — se a duração de uma ultrapassa o início da próxima, elas empilham
            # visualmente. Limitamos pelo início do próximo destaque, com uma folga de
            # 0.15s pra garantir que um sai antes do outro entrar. Piso de 0.45s porque é
            # o mínimo pro pop-in (0.18s) + fade-out (0.25s) ainda renderizarem direito.
            if idx + 1 < len(destaques_ordenados):
                espaco_disponivel = destaques_ordenados[idx + 1]['inicio'] - destaque['inicio'] - 0.15
                duracao = max(0.45, min(duracao, espaco_disponivel))

            txt_clip = txt_clip.set_duration(duracao)
            txt_clip = txt_clip.resize(_escala).set_position(_posicao)
            txt_clip = txt_clip.set_start(offset + destaque['inicio'])
            txt_clip = txt_clip.crossfadein(DURACAO_POP_DESTAQUE).crossfadeout(0.25)
            clips.append(txt_clip)
        except Exception as e:
            print(f"  ⚠️ Erro ao gerar destaque '{destaque.get('texto')}': {e} — pulando")

    if clips:
        print(f"✨ {len(clips)} destaque(s) visual(is) gerado(s) (efeito pop + fade in/out)")
    return clips


# ============================================================
# PEXELS — busca e download de vídeos, com limite de duração por clipe
# ============================================================

def escolher_termo_pesquisa(tema, roteiro):
    termos_validados = config.get('termos_pesquisa_validados', [])
    if not termos_validados:
        raise Exception("config.json precisa ter 'termos_pesquisa_validados' preenchido")

    prompt = f"""Tema do vídeo: "{tema}"
Trecho do roteiro: "{roteiro[:300]}"

Escolha o termo MAIS adequado desta lista pré-aprovada (responda EXATAMENTE um item da lista, sem alterar
o texto, sem aspas, sem explicação):

{json.dumps(termos_validados, ensure_ascii=False)}"""

    resposta = _gemini_generate(prompt)
    termo_escolhido = resposta.text.strip().strip('"').strip("'")

    if termo_escolhido not in termos_validados:
        print(f"  ⚠️ Gemini retornou termo fora da lista ('{termo_escolhido}') — usando aleatório da lista")
        termo_escolhido = random.choice(termos_validados)

    print(f"🔍 Termo de busca escolhido: {termo_escolhido}")
    return termo_escolhido


def _escolher_arquivo_video(video_files, largura_alvo):
    candidatos = [vf for vf in video_files if vf.get('width') and vf.get('height')]
    if not candidatos:
        return None
    return min(candidatos, key=lambda vf: abs(vf['width'] - largura_alvo))


def pesquisar_videos_pexels(termo, orientacao, pagina=1, por_pagina=40):
    headers = {"Authorization": PEXELS_API_KEY}
    params = {
        "query": termo, "orientation": orientacao, "per_page": por_pagina,
        "page": pagina, "min_duration": 4,
    }
    resp = requests.get("https://api.pexels.com/videos/search", headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get('videos', [])


PEXELS_LOG_FILE = 'pexels_usados.json'
DIAS_EVITAR_REPETICAO_PEXELS = int(os.environ.get('DIAS_EVITAR_REPETICAO_PEXELS', '20'))


def _carregar_pexels_usados_recentes():
    """Retorna o set de IDs de vídeo do Pexels usados nos últimos N dias (evita repetir entre publicações)."""
    if not os.path.exists(PEXELS_LOG_FILE):
        return set()
    try:
        with open(PEXELS_LOG_FILE, 'r', encoding='utf-8') as f:
            registros = json.load(f)
    except Exception:
        return set()

    limite = datetime.now().timestamp() - (DIAS_EVITAR_REPETICAO_PEXELS * 86400)
    return {r['id'] for r in registros if r.get('data', 0) >= limite}


def _salvar_pexels_usado(video_id):
    """Adiciona um vídeo ao histórico, descartando registros mais antigos que a janela de dias."""
    registros = []
    if os.path.exists(PEXELS_LOG_FILE):
        try:
            with open(PEXELS_LOG_FILE, 'r', encoding='utf-8') as f:
                registros = json.load(f)
        except Exception:
            registros = []

    agora = datetime.now().timestamp()
    limite = agora - (DIAS_EVITAR_REPETICAO_PEXELS * 86400)
    registros = [r for r in registros if r.get('data', 0) >= limite]
    registros.append({'id': video_id, 'data': agora})

    with open(PEXELS_LOG_FILE, 'w', encoding='utf-8') as f:
        json.dump(registros, f, indent=2, ensure_ascii=False)


def baixar_clipes_pexels(termo, orientacao, duracao_alvo, offset_inicio=0.0):
    """
    Baixa vídeos do Pexels sequencialmente até cobrir duracao_alvo (segundos).
    Cada clipe usa no máximo DURACAO_MAXIMA_CLIPE segundos (mesmo que o vídeo fonte seja mais longo),
    o que aumenta a variedade de cortes e reduz a duração de cada vídeo repetido entre shorts.
    Nunca repete o mesmo vídeo dentro do mesmo short (usados_ids é local a esta chamada), e evita
    reusar vídeos já usados nos últimos DIAS_EVITAR_REPETICAO_PEXELS dias (histórico persistido).
    Busca páginas adicionais automaticamente se a primeira não tiver candidatos suficientes.
    """
    largura_alvo = 1080 if orientacao == 'portrait' else 1920
    os.makedirs(f'{ASSETS_DIR}/pexels', exist_ok=True)

    usados_recentemente = _carregar_pexels_usados_recentes()
    print(f"  📋 {len(usados_recentemente)} vídeo(s) no histórico dos últimos "
          f"{DIAS_EVITAR_REPETICAO_PEXELS} dias (serão evitados)")

    clipes = []
    tempo_coberto = 0.0
    usados_ids = set()
    pagina = 1
    MAX_PAGINAS = 8  # aumentado: com o histórico filtrando candidatos, pode precisar de mais páginas

    while tempo_coberto < duracao_alvo and pagina <= MAX_PAGINAS:
        if LIMITE_CLIPES_TESTE > 0 and len(clipes) >= LIMITE_CLIPES_TESTE:
            print(f"   ✂️ MODO TESTE: limitado a {LIMITE_CLIPES_TESTE} clipe(s)")
            break

        videos_encontrados = pesquisar_videos_pexels(termo, orientacao, pagina=pagina)
        if not videos_encontrados:
            print(f"  ⚠️ Página {pagina} sem resultados para '{termo}' ({orientacao})")
            break

        random.shuffle(videos_encontrados)

        for video in videos_encontrados:
            if tempo_coberto >= duracao_alvo:
                break
            if LIMITE_CLIPES_TESTE > 0 and len(clipes) >= LIMITE_CLIPES_TESTE:
                break

            video_id = video.get('id')
            if video_id in usados_ids:
                continue
            if video_id in usados_recentemente:
                continue
            usados_ids.add(video_id)

            arquivo = _escolher_arquivo_video(video.get('video_files', []), largura_alvo)
            if not arquivo:
                continue

            destino = f"{ASSETS_DIR}/pexels/{video_id}.mp4"
            try:
                print(f"  ⬇️ Baixando vídeo {video_id} ({video.get('duration')}s, "
                      f"{arquivo['width']}x{arquivo['height']})...")
                resp = requests.get(arquivo['link'], timeout=60)
                resp.raise_for_status()
                with open(destino, 'wb') as f:
                    f.write(resp.content)
            except Exception as e:
                print(f"  ⚠️ Erro ao baixar vídeo {video_id}: {e}")
                continue

            duracao_disponivel = video.get('duration', 6)
            duracao_uso = min(duracao_disponivel, DURACAO_MAXIMA_CLIPE, duracao_alvo - tempo_coberto)

            clipes.append({'path': destino, 'inicio': offset_inicio + tempo_coberto, 'duracao': duracao_uso})
            tempo_coberto += duracao_uso
            _salvar_pexels_usado(video_id)

        pagina += 1

    if tempo_coberto < duracao_alvo and clipes:
        print(f"  ⚠️ Cobertura parcial: {tempo_coberto:.1f}s/{duracao_alvo:.1f}s — "
              f"último clipe será esticado na montagem")

    print(f"  ✅ {len(clipes)} clipe(s) baixado(s) (máx {DURACAO_MAXIMA_CLIPE}s cada), "
          f"cobrindo {tempo_coberto:.1f}s de {duracao_alvo:.1f}s")
    return clipes


def _duracao_real_arquivo(caminho):
    """ffprobe rápido (só lê o cabeçalho/container, não decodifica frames) pra pegar a
    duração REAL de um arquivo de vídeo já baixado — usado como teto de segurança contra
    o metadado 'duration' da API do Pexels divergir da variante de arquivo baixada.
    Retorna None se ffprobe falhar/não estiver disponível — nesse caso o código volta a
    confiar só no metadado da API (comportamento antigo), sem quebrar o vídeo."""
    import subprocess
    try:
        resultado = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', caminho],
            capture_output=True, text=True, timeout=10
        )
        return float(resultado.stdout.strip())
    except Exception:
        return None


def baixar_clipes_por_bloco(blocos_com_tempo, orientacao):
    """
    Fase 2 — B-roll casado por BLOCO do roteiro, não pelo vídeo inteiro: cada bloco
    (gancho, evidência, objeção...) já chegou aqui com seu próprio 'termo' (escolhido por
    producao_visual.escolher_termos_por_bloco) e sua própria janela de tempo dentro da
    narração (bloco['inicio']/['duracao'], de mapear_tempos_para_blocos). Cada clipe baixado
    já nasce posicionado no tempo certo — sem isso, o vídeo inteiro usava só 1 termo de busca.
    Compartilha o histórico anti-repetição do Pexels entre todos os blocos do mesmo vídeo,
    pra não repetir o mesmo vídeo em blocos diferentes do mesmo short/vídeo longo.
    Se o roteiro caiu no fallback simples (1 bloco só, 'roteiro_completo'), isso se comporta
    exatamente como a antiga baixar_clipes_pexels() — degrada graciosamente, não quebra nada.
    """
    largura_alvo = 1080 if orientacao == 'portrait' else 1920
    os.makedirs(f'{ASSETS_DIR}/pexels', exist_ok=True)

    usados_recentemente = _carregar_pexels_usados_recentes()
    usados_no_video = set()
    print(f"  📋 {len(usados_recentemente)} vídeo(s) no histórico dos últimos "
          f"{DIAS_EVITAR_REPETICAO_PEXELS} dias (serão evitados)")

    todos_os_clipes = []
    for i, bloco in enumerate(blocos_com_tempo):
        termo = bloco['termo']
        duracao_alvo = bloco['duracao']
        offset_bloco = bloco['inicio']

        print(f"  🎯 Bloco '{bloco['bloco']}' ({duracao_alvo:.1f}s) — termo: '{termo}'")

        # Webdoc, opt-in via config.json ('usar_prints_noticia': true): em vez de buscar
        # B-roll no Pexels pra esse bloco, gera um mockup de print de notícia (mockups_visuais.py)
        # com a manchete decidida em decidir_prints_de_noticia(). Nada disso roda se a flag
        # não estiver ligada — bloco.get() volta None e cai direto no fluxo antigo abaixo.
        if bloco.get('usa_print_noticia'):
            try:
                # BUGFIX (print ficando ~1min na tela): antes usava duracao_alvo (a
                # duração INTEIRA do bloco de narração) pro print inteiro — se o bloco
                # tinha 40-60s de fala, o print ficava parado na tela esse tempo todo.
                # Cap: só o tempo que o roteiro realmente está "falando sobre" a
                # matéria (config 'duracao_maxima_print_noticia', default 9s) — o
                # RESTO do bloco cai pro fluxo normal de B-roll logo abaixo (mesmo
                # while loop, só que começando com tempo_coberto > 0 em vez de 0).
                duracao_print = min(duracao_alvo, DURACAO_MAXIMA_PRINT_NOTICIA)
                caminho_print = gerar_print_noticia(
                    manchete=bloco['manchete_noticia'],
                    subtitulo=bloco.get('subtitulo_noticia'),
                    output_path=f"{ASSETS_DIR}/prints/bloco_{i}.png",
                )
                todos_os_clipes.append({
                    'path': caminho_print,
                    'inicio': offset_bloco,
                    'duracao': duracao_print,
                    'transicao_bloco': not bloco.get('inicio_capitulo'),
                })
                print(f"    📰 Print de notícia gerado ({duracao_print:.1f}s): \"{bloco['manchete_noticia']}\"")
                tempo_coberto = duracao_print  # o resto do bloco (se sobrar) vira B-roll normal abaixo
            except Exception as e:
                print(f"    ⚠️ Falha ao gerar print de notícia ({e}) — caindo pro B-roll normal")
                tempo_coberto = 0.0
        else:
            tempo_coberto = 0.0

        pagina = 1
        while tempo_coberto < duracao_alvo and pagina <= MAX_PAGINAS_BLOCO:
            if LIMITE_CLIPES_TESTE > 0 and len(todos_os_clipes) >= LIMITE_CLIPES_TESTE:
                print(f"   ✂️ MODO TESTE: limitado a {LIMITE_CLIPES_TESTE} clipe(s) no total")
                return todos_os_clipes

            # Diversidade de mídia (config 'pesos_fontes_midia'): antes de buscar mais
            # vídeo no Pexels, sorteia se esse "slot" deveria vir de Wikimedia/Internet
            # Archive/Agnes — sem isso configurado, sempre cai no Pexels normal (100%
            # compatível com o comportamento antigo). Cada slot alternativo dura entre
            # 10 e 14s (config 'duracao_slot_midia_alternativa_min/max') — dinamismo,
            # sem ficar muito tempo parado numa imagem só.
            caminho_alt, fonte_alt = None, None
            if duracao_alvo - tempo_coberto >= 3:  # não vale a pena buscar fonte alternativa pra um resto minúsculo
                caminho_alt, fonte_alt = _escolher_fonte_midia_alternativa(termo)
            if caminho_alt:
                min_s = config.get('duracao_slot_midia_alternativa_min', 10)
                max_s = config.get('duracao_slot_midia_alternativa_max', 14)
                duracao_slot = min(random.uniform(min_s, max_s), duracao_alvo - tempo_coberto)
                todos_os_clipes.append({
                    'path': caminho_alt,
                    'inicio': offset_bloco + tempo_coberto,
                    'duracao': duracao_slot,
                    'transicao_bloco': (tempo_coberto == 0.0) and not bloco.get('inicio_capitulo'),
                    'fonte': fonte_alt,
                })
                tempo_coberto += duracao_slot
                continue  # próxima volta do while decide de novo (Pexels ou outra fonte)

            videos_encontrados = pesquisar_videos_pexels(termo, orientacao, pagina=pagina)
            if not videos_encontrados:
                print(f"    ⚠️ Página {pagina} sem resultados para '{termo}'")
                break
            random.shuffle(videos_encontrados)

            for video in videos_encontrados:
                if tempo_coberto >= duracao_alvo:
                    break
                if LIMITE_CLIPES_TESTE > 0 and len(todos_os_clipes) >= LIMITE_CLIPES_TESTE:
                    break

                video_id = video.get('id')
                if video_id in usados_no_video or video_id in usados_recentemente:
                    continue

                arquivo = _escolher_arquivo_video(video.get('video_files', []), largura_alvo)
                if not arquivo:
                    continue

                destino = f"{ASSETS_DIR}/pexels/{video_id}.mp4"
                try:
                    resp = requests.get(arquivo['link'], timeout=60)
                    resp.raise_for_status()
                    with open(destino, 'wb') as f:
                        f.write(resp.content)
                except Exception as e:
                    print(f"    ⚠️ Erro ao baixar vídeo {video_id}: {e}")
                    continue

                usados_no_video.add(video_id)
                # BUGFIX (tela preta ~1s na transição): a duração que a API do Pexels
                # informa ('duration' no JSON) é do vídeo mestre, não necessariamente da
                # variante/resolução específica que baixamos em 'arquivo['link']' — quando
                # elas divergem, o clipe real acaba mais curto que o previsto e o próximo
                # corte fica agendado tarde demais, deixando um vão sem nada desenhado
                # (preto) entre o fim do clipe real e o início do próximo. Medimos a
                # duração REAL do arquivo já em disco (ffprobe, rápido, não decodifica
                # o vídeo inteiro) e usamos ela como teto — nunca confiamos só no metadado.
                duracao_real = _duracao_real_arquivo(destino)
                duracao_disponivel = video.get('duration', 6)
                if duracao_real is not None and duracao_real < duracao_disponivel:
                    print(f"    ℹ️ Duração real do arquivo ({duracao_real:.2f}s) menor que "
                          f"a informada pela API ({duracao_disponivel:.2f}s) — usando a real "
                          f"pra não deixar vão preto na transição")
                    duracao_disponivel = duracao_real
                duracao_uso = min(duracao_disponivel, DURACAO_MAXIMA_CLIPE, duracao_alvo - tempo_coberto)

                todos_os_clipes.append({
                    'path': destino,
                    'inicio': offset_bloco + tempo_coberto,
                    'duracao': duracao_uso,
                    # 1º clipe do bloco leva transição (crossfade) — EXCETO quando o
                    # bloco abre um capítulo (modo webdoc): ali a troca já é coberta
                    # pelo card preto + vinheta silenciosos, um crossfade "vazaria"
                    # B-roll pra dentro do silêncio antes do card acabar.
                    'transicao_bloco': (tempo_coberto == 0.0) and not bloco.get('inicio_capitulo'),
                })
                tempo_coberto += duracao_uso
                _salvar_pexels_usado(video_id)

            pagina += 1

        if tempo_coberto < duracao_alvo:
            print(f"    ⚠️ Cobertura parcial do bloco: {tempo_coberto:.1f}s/{duracao_alvo:.1f}s")

    print(f"  ✅ {len(todos_os_clipes)} clipe(s) baixado(s) no total, casados por bloco do roteiro")
    return todos_os_clipes


# ============================================================
# MONTAGEM DE VÍDEO
# ============================================================

DURACAO_TRANSICAO = float(os.environ.get('DURACAO_TRANSICAO', '0.7'))  # crossfade entre blocos, em segundos

# Quanto antecipar o SFX de transição (woosh) em relação ao instante teórico do corte
# (tempo_corte). AJUSTE: o crossfade visual começa DURACAO_TRANSICAO segundos ANTES de
# tempo_corte e só termina de aparecer NELE — um whoosh soa natural quando o INÍCIO do
# som coincide com o INÍCIO do movimento visual, não com um ponto no meio do efeito.
# Por isso o padrão agora é igual a DURACAO_TRANSICAO (som e crossfade começam juntos,
# e terminam praticamente juntos também, já que o woosh.mp3 aparado tem ~0.6s de conteúdo
# audível pra um crossfade de 0.7s). Ajustável via config.json ('antecipacao_sfx_transicao')
# se quiser algo diferente — 0 desativa a antecipação (comportamento anterior ao fix).
ANTECIPACAO_SFX_TRANSICAO = float(os.environ.get(
    'ANTECIPACAO_SFX_TRANSICAO', config.get('antecipacao_sfx_transicao', DURACAO_TRANSICAO)
))


TRANSICOES_VIDEO = config.get('transicoes_video', ['crossfade'])  # ['crossfade','flash','glitch','shadow_wipe']


def _clip_de_imagem_com_zoom(caminho_imagem, duracao, largura, altura, zoom_final=1.08, zoom_inicial=1.0):
    """
    Ken Burns simples (zoom lento e contínuo) pra imagens estáticas. zoom_inicial <
    zoom_final = zoom IN (padrão, usado nos prints de notícia); zoom_inicial > zoom_final
    = zoom OUT (usado nas fotos de arquivo do Wikimedia/Internet Archive, ver
    _clip_de_imagem_vintage) — dá uma sensação diferente, mais "revelando a cena",
    reforçando que aquela imagem é de outra fonte (arquivo/histórica), não B-roll comum.
    """
    img_clip = ImageClip(caminho_imagem).set_duration(duracao)
    if img_clip.w / img_clip.h > largura / altura:
        img_clip = img_clip.resize(height=altura)
    else:
        img_clip = img_clip.resize(width=largura)

    def _fator_zoom(t):
        progresso = t / duracao if duracao > 0 else 1
        return zoom_inicial + (zoom_final - zoom_inicial) * progresso

    img_clip = img_clip.resize(_fator_zoom).set_position('center')
    return CompositeVideoClip([img_clip], size=(largura, altura)).set_duration(duracao)


def _aplicar_vinheta_vintage(caminho_imagem, output_path, intensidade=0.55):
    """
    Escurece as bordas (vinheta radial) e reduz levemente saturação/contraste — efeito
    'vintage' pra diferenciar visualmente fotos de arquivo (Wikimedia Commons/Internet
    Archive) do B-roll de vídeo comum, reforçando a sensação de material histórico/de
    apuração em vez de imagem de banco genérico.
    """
    from PIL import ImageEnhance

    img = Image.open(caminho_imagem).convert('RGB')
    w, h = img.size

    img = ImageEnhance.Color(img).enhance(0.75)
    img = ImageEnhance.Contrast(img).enhance(1.08)
    img = ImageEnhance.Brightness(img).enhance(0.95)

    vinheta = Image.new('L', (w, h), 0)
    draw_v = ImageDraw.Draw(vinheta)
    max_raio = int(((w ** 2 + h ** 2) ** 0.5) / 2)
    passo = max(1, max_raio // 120)  # ~120 aneis, suficiente pra suavidade sem ser lento
    # alpha ALTO (255, imagem visível) no CENTRO, alpha BAIXO (mais preto) nas BORDAS —
    # desenha do maior raio (borda) pro menor (centro), então o centro fica por cima.
    for r in range(max_raio, 0, -passo):
        alpha = int(255 * (1 - intensidade * (r / max_raio)))
        alpha = max(0, min(255, alpha))
        draw_v.ellipse([w / 2 - r, h / 2 - r, w / 2 + r, h / 2 + r], fill=alpha)

    preto = Image.new('RGB', (w, h), (0, 0, 0))
    img = Image.composite(img, preto, vinheta)
    img.save(output_path, quality=92)
    return output_path


def _clip_de_imagem_vintage(caminho_imagem, duracao, largura, altura):
    """Zoom lento PRA FORA + vinheta vintage — usado só pras imagens de arquivo real
    (Wikimedia Commons / Internet Archive), nunca pro B-roll de vídeo comum."""
    caminho_vintage = os.path.splitext(caminho_imagem)[0] + '_vintage.jpg'
    _aplicar_vinheta_vintage(caminho_imagem, caminho_vintage)
    return _clip_de_imagem_com_zoom(caminho_vintage, duracao, largura, altura,
                                     zoom_inicial=1.15, zoom_final=1.0)


def buscar_imagem_wikimedia(termo, output_dir=None):
    """
    Busca uma imagem no Wikimedia Commons (100% grátis, sem chave) relacionada ao termo.
    Não precisamos filtrar licença manualmente — é condição de hospedagem no Commons que
    todo arquivo já tenha licença livre (domínio público ou Creative Commons). Retorna o
    caminho do arquivo baixado, ou None se não achar nada usável (o chamador cai pro
    Pexels sem quebrar o vídeo).
    """
    output_dir = output_dir or f'{ASSETS_DIR}/wikimedia'
    os.makedirs(output_dir, exist_ok=True)
    try:
        params = {
            'action': 'query', 'generator': 'search',
            'gsrsearch': f'{termo} filetype:bitmap', 'gsrnamespace': 6, 'gsrlimit': 10,
            'prop': 'imageinfo', 'iiprop': 'url|mime', 'iiurlwidth': 1920, 'format': 'json',
        }
        resp = requests.get('https://commons.wikimedia.org/w/api.php', params=params, timeout=20,
                             headers={'User-Agent': 'WebdocPipeline/1.0 (uso educacional/canal YouTube)'})
        resp.raise_for_status()
        paginas = list(resp.json().get('query', {}).get('pages', {}).values())
        random.shuffle(paginas)

        for pagina in paginas:
            info = (pagina.get('imageinfo') or [None])[0]
            if not info:
                continue
            mime = info.get('mime', '')
            if not mime.startswith('image/') or mime == 'image/svg+xml':
                continue  # SVG (mapa/diagrama vetorial) não presta como B-roll fotográfico
            url = info.get('thumburl') or info.get('url')
            if not url:
                continue
            destino = os.path.join(output_dir, f"wm_{pagina.get('pageid')}.jpg")
            img_resp = requests.get(url, timeout=30)
            img_resp.raise_for_status()
            with open(destino, 'wb') as f:
                f.write(img_resp.content)
            print(f"    🖼️ Wikimedia Commons: \"{pagina.get('title', '')}\"")
            return destino
    except Exception as e:
        print(f"    ⚠️ Wikimedia Commons falhou ({e})")
    return None


def buscar_imagem_internet_archive(termo, output_dir=None):
    """
    Busca uma imagem de domínio público/licença livre no Internet Archive relacionada
    ao termo — bom pra material de arquivo/histórico que não existe em banco de vídeo
    stock comum. 100% grátis, sem chave. Retorna o caminho baixado ou None.
    """
    output_dir = output_dir or f'{ASSETS_DIR}/internet_archive'
    os.makedirs(output_dir, exist_ok=True)
    try:
        params = {'q': f'{termo} AND mediatype:image', 'rows': 10, 'output': 'json'}
        params['fl[]'] = ['identifier', 'title']
        resp = requests.get('https://archive.org/advancedsearch.php', params=params, timeout=20)
        resp.raise_for_status()
        docs = resp.json().get('response', {}).get('docs', [])
        random.shuffle(docs)

        for doc in docs:
            identifier = doc.get('identifier')
            if not identifier:
                continue
            meta_resp = requests.get(f'https://archive.org/metadata/{identifier}', timeout=20)
            meta_resp.raise_for_status()
            arquivos = meta_resp.json().get('files', [])
            candidatos = [a for a in arquivos
                          if a.get('name', '').lower().endswith(('.jpg', '.jpeg', '.png'))
                          and a.get('source') == 'original']
            if not candidatos:
                continue
            arquivo = random.choice(candidatos)
            url = f"https://archive.org/download/{identifier}/{arquivo['name']}"
            destino = os.path.join(output_dir, f"ia_{identifier}_{os.path.basename(arquivo['name'])}")
            img_resp = requests.get(url, timeout=30)
            img_resp.raise_for_status()
            with open(destino, 'wb') as f:
                f.write(img_resp.content)
            print(f"    📼 Internet Archive: \"{doc.get('title', identifier)}\"")
            return destino
    except Exception as e:
        print(f"    ⚠️ Internet Archive falhou ({e})")
    return None


def _escolher_fonte_midia_alternativa(termo):
    """
    Sorteia (com pesos configuráveis) se o próximo "slot" de B-roll de um bloco deve vir
    de uma fonte alternativa ao Pexels — Wikimedia Commons, Internet Archive ou Agnes AI
    (gerada). Retorna (caminho_arquivo, fonte) ou (None, None) se sorteou Pexels, se a
    fonte sorteada falhou, ou se nenhum peso > 0 estiver configurado. O chamador SEMPRE
    tem o Pexels como fallback — isso aqui só decide se tenta OUTRA coisa PRIMEIRO.

    Pesos em config.json → 'pesos_fontes_midia': {"pexels": 0.5, "wikimedia": 0.2,
    "internet_archive": 0.15, "agnes": 0.15}. Sem essa chave, o padrão é 100% Pexels
    (comportamento antigo, opt-in pra diversidade de fonte).
    """
    pesos = config.get('pesos_fontes_midia', {'pexels': 1.0})
    fontes = list(pesos.keys())
    valores = list(pesos.values())
    if sum(valores) <= 0:
        return None, None
    escolhida = random.choices(fontes, weights=valores, k=1)[0]

    if escolhida == 'pexels':
        return None, None
    if escolhida == 'wikimedia':
        caminho = buscar_imagem_wikimedia(termo)
        return (caminho, 'wikimedia') if caminho else (None, None)
    if escolhida == 'internet_archive':
        caminho = buscar_imagem_internet_archive(termo)
        return (caminho, 'internet_archive') if caminho else (None, None)
    if escolhida == 'agnes':
        caminho, e_video = gerar_midia_agnes(
            termo, f"{ASSETS_DIR}/agnes",
            tentar_video=config.get('agnes_gerar_video', False)
        )
        if not caminho:
            return None, None
        return caminho, ('agnes_video' if e_video else 'agnes')
    return None, None


def _preparar_clip_pexels(item, largura, altura):
    if item['path'].lower().endswith(('.png', '.jpg', '.jpeg')):
        # Wikimedia/Internet Archive usam zoom-out + vinheta vintage (ver
        # _escolher_fonte_midia_alternativa) — Agnes e print de notícia usam o zoom-in
        # normal, porque são imagens "novas"/geradas, não material de arquivo.
        if item.get('fonte') in ('wikimedia', 'internet_archive'):
            return _clip_de_imagem_vintage(item['path'], item['duracao'], largura, altura).set_start(item['inicio'])
        return _clip_de_imagem_com_zoom(item['path'], item['duracao'], largura, altura).set_start(item['inicio'])

    clip = VideoFileClip(item['path'])
    if clip.duration > item['duracao']:
        clip = clip.subclip(0, item['duracao'])

    clip = clip.resize(height=altura)
    if clip.w > largura:
        clip = clip.crop(x_center=clip.w / 2, width=largura, height=altura)
    elif clip.w < largura:
        clip = clip.resize(width=largura)
    if clip.size != (largura, altura):
        clip = clip.resize((largura, altura))

    return clip.without_audio().set_start(item['inicio'])


def _montar_clips_pexels(lista_clipes, largura, altura):
    """
    Corte seco dentro do mesmo bloco (vídeo real já tem movimento próprio, não precisa
    de transição). Transição (crossfade ou outra, ver transicoes.py) só na TROCA de
    bloco do roteiro — é o que dá a sensação de "capítulo novo" em vez de só mais um
    corte de B-roll.
    Qual transição usar é escolhida (aleatoriamente, se houver mais de uma) a partir da
    lista em config.json 'transicoes_video' — default ['crossfade'] mantém o
    comportamento antigo pra canais que não configuraram nada. Se a transição
    escolhida falhar ao renderizar (efeito novo, mais frágil que o dissolve simples),
    cai pro crossfade sem derrubar o vídeo inteiro.
    """
    clips_prontos = []
    transicoes_aplicadas = 0
    contagem_por_tipo = {}
    for i, item in enumerate(lista_clipes):
        try:
            clip = _preparar_clip_pexels(item, largura, altura)
            if i > 0 and item.get('transicao_bloco') and clips_prontos:
                nome_transicao = random.choice(TRANSICOES_VIDEO) if TRANSICOES_VIDEO else 'crossfade'
                funcao_transicao = TRANSICOES_DISPONIVEIS.get(nome_transicao, transicao_crossfade)
                try:
                    clip_anterior, clip = funcao_transicao(
                        clips_prontos[-1], clip, item['inicio'], DURACAO_TRANSICAO
                    )
                except Exception as e:
                    print(f"  ⚠️ Transição '{nome_transicao}' falhou ({e}) — usando crossfade")
                    nome_transicao = 'crossfade'
                    clip_anterior, clip = transicao_crossfade(
                        clips_prontos[-1], clip, item['inicio'], DURACAO_TRANSICAO
                    )
                clips_prontos[-1] = clip_anterior
                transicoes_aplicadas += 1
                contagem_por_tipo[nome_transicao] = contagem_por_tipo.get(nome_transicao, 0) + 1
            clips_prontos.append(clip)
        except Exception as e:
            print(f"  ⚠️ Erro ao preparar clipe {i}: {e}")
    resumo_tipos = ", ".join(f"{n}x {tipo}" for tipo, n in contagem_por_tipo.items())
    print(f"  🔀 {transicoes_aplicadas} transição(ões) de {DURACAO_TRANSICAO}s aplicada(s) entre "
          f"{len(clips_prontos)} clipe(s)" + (f" ({resumo_tipos})" if resumo_tipos else "") +
          " — 0 é normal se o vídeo não chegou a trocar de bloco "
          "(ex: teste com LIMITE_CLIPES_TESTE baixo, cobrindo só o 1º bloco)")
    return clips_prontos




def _mixar_musica_fundo(audio_narracao, duracao_total, volume=0.06, musicas_dir='assets/musicas'):
    import glob
    from moviepy.editor import AudioFileClip, CompositeAudioClip

    musicas = (glob.glob(f'{musicas_dir}/*.mp3') + glob.glob(f'{musicas_dir}/*.wav') +
               glob.glob(f'{musicas_dir}/*.ogg'))
    if not musicas:
        print("  ⚠️ Nenhuma música encontrada em assets/musicas/ — sem fundo")
        return audio_narracao

    musica_escolhida = random.choice(musicas)
    print(f"  🎼 Música: {os.path.basename(musica_escolhida)} (volume {int(volume * 100)}%)")
    musica = AudioFileClip(musica_escolhida)

    if musica.duration < duracao_total:
        import math
        from moviepy.editor import concatenate_audioclips
        repeticoes = math.ceil(duracao_total / musica.duration)
        musica = concatenate_audioclips([musica] * repeticoes)

    musica = musica.subclip(0, duracao_total).volumex(volume)
    return CompositeAudioClip([audio_narracao, musica])


def _mixar_musica_por_capitulo(audio_narracao, duracao_total, marcos_capitulos, volume=0.06,
                                musicas_dir='assets/musicas', crossfade=1.5):
    """
    Versão em capítulos do _mixar_musica_fundo: em vez de UMA faixa tocando o vídeo
    inteiro, sorteia uma faixa DIFERENTE pra cada trecho entre marcos_capitulos (cada
    capítulo tem sua própria "música tema", com crossfade suave na troca) — é o que dá
    o "ritmo que muda por capítulo" descrito no formato Elementar.

    marcos_capitulos: lista de instantes (float, segundos, já no tempo final do vídeo —
    ou seja, já somado offset_narracao) em que um novo capítulo começa. O primeiro
    trecho (introdução, antes do primeiro marco) também sorteia sua própria faixa.

    Se assets/musicas/ tiver menos faixas que capítulos, repete faixas (sem travar o
    pipeline) — mas avisa, porque idealmente cada capítulo tem uma faixa distinta.
    Se marcos_capitulos vier vazio, cai pro comportamento de _mixar_musica_fundo (uma
    faixa só) — modo webdoc sem capítulos detectados não quebra.
    """
    import glob
    import math
    from moviepy.editor import AudioFileClip, CompositeAudioClip, concatenate_audioclips

    if not marcos_capitulos:
        return _mixar_musica_fundo(audio_narracao, duracao_total, volume, musicas_dir)

    musicas = (glob.glob(f'{musicas_dir}/*.mp3') + glob.glob(f'{musicas_dir}/*.wav') +
               glob.glob(f'{musicas_dir}/*.ogg'))
    if not musicas:
        print("  ⚠️ Nenhuma música encontrada em assets/musicas/ — sem fundo")
        return audio_narracao

    limites = [0.0] + sorted(marcos_capitulos) + [duracao_total]
    n_trechos = len(limites) - 1

    pool = musicas.copy()
    random.shuffle(pool)
    if len(pool) < n_trechos:
        print(f"  ⚠️ Só {len(pool)} música(s) em {musicas_dir}/ pra {n_trechos} trecho(s) — "
              f"algumas vão repetir (ideal: 1 faixa distinta por capítulo)")
        while len(pool) < n_trechos:
            pool += musicas
    faixas_por_trecho = pool[:n_trechos]

    trechos_audio = []
    for i in range(n_trechos):
        inicio_trecho, fim_trecho = limites[i], limites[i + 1]
        duracao_trecho = max(0.1, fim_trecho - inicio_trecho)
        # folga de 'crossfade' extra no fim de cada trecho (exceto o último) pra sobrar
        # material real pra sobrepor na transição, em vez de repetir silêncio
        duracao_com_folga = duracao_trecho + (crossfade if i < n_trechos - 1 else 0)

        musica = AudioFileClip(faixas_por_trecho[i])
        if musica.duration < duracao_com_folga:
            repeticoes = math.ceil(duracao_com_folga / musica.duration)
            musica = concatenate_audioclips([musica] * repeticoes)
        musica = musica.subclip(0, duracao_com_folga).volumex(volume)
        print(f"  🎼 Capítulo {i + 1}: {os.path.basename(faixas_por_trecho[i])} "
              f"(t={inicio_trecho:.1f}s–{fim_trecho:.1f}s)")

        # BUGFIX: crossfadein/crossfadeout só existem em CLIPES DE VÍDEO no MoviePy
        # (mexem em máscara/transparência) — em áudio o nome certo é audio_fadein/
        # audio_fadeout. Aplicamos fade-out na cauda de todo trecho que não é o
        # último, e fade-in na cabeça de todo trecho que não é o primeiro — as duas
        # pontas se sobrepõem na janela de 'crossfade' segundos, dando o crossfade
        # de verdade (sem isso, só a entrada suaviza e a saída corta seco).
        if i > 0:
            musica = musica.audio_fadein(crossfade)
        if i < n_trechos - 1:
            musica = musica.audio_fadeout(crossfade)
        trechos_audio.append(musica.set_start(inicio_trecho))

    return CompositeAudioClip([audio_narracao] + trechos_audio)


def aplicar_sfx(audio_base, eventos_sfx, offset=0.0, sfx_dir='assets/sfx'):
    """
    Fase 2 — camada de SFX orientada a evento, mesmo espírito de fallback gracioso do
    _mixar_musica_fundo: toca um som curto a cada troca de bloco (evento 'transicao')
    e a cada destaque visual que aparece (evento 'destaque'). Se não achar arquivo,
    essa camada simplesmente não ativa — não quebra o vídeo.

    Estrutura recomendada (efeitos curtos, licença livre — ex: CC0 do Freesound):
        assets/sfx/transicao/*.mp3|*.wav|*.ogg  (ex: whoosh curto, corte seco)
        assets/sfx/destaque/*.mp3|*.wav|*.ogg   (ex: pop, ping, impacto leve)
    Se essas subpastas não existirem/estiverem vazias, cai pra um pool único direto
    em assets/sfx/*.* (compartilhado entre os dois tipos de evento) — pra não exigir
    a estrutura de subpastas logo no primeiro teste.
    """
    if not ATIVAR_SFX or not eventos_sfx:
        return audio_base

    import glob
    from moviepy.editor import AudioFileClip, CompositeAudioClip

    extensoes = ('*.mp3', '*.wav', '*.ogg', '*.m4a')

    def _listar(pasta):
        arquivos = []
        for ext in extensoes:
            arquivos += glob.glob(f'{pasta}/{ext}')
        return arquivos

    bibliotecas = {tipo: _listar(f'{sfx_dir}/{tipo}') for tipo in ('transicao', 'destaque')}

    if not any(bibliotecas.values()):
        pool_flat = _listar(sfx_dir)
        if pool_flat:
            print(f"  ℹ️ Sem subpastas transicao/destaque — usando os {len(pool_flat)} "
                  f"arquivo(s) soltos em {sfx_dir}/ como pool único pra ambos os eventos")
            bibliotecas = {'transicao': pool_flat, 'destaque': pool_flat}
        else:
            print(f"  ℹ️ Nenhum SFX encontrado em {sfx_dir}/ — seguindo sem camada de som de eventos")
            return audio_base

    camadas = [audio_base]
    aplicados = 0
    for evento in eventos_sfx:
        arquivos = bibliotecas.get(evento['tipo'], [])
        if not arquivos:
            continue
        arquivo = random.choice(arquivos)
        try:
            # BUGFIX (woosh "atrasado"): o corte visual (crossfade) começa
            # DURACAO_TRANSICAO segundos ANTES do instante 'tempo' e só termina de
            # aparecer NO instante 'tempo' — se o SFX toca exatamente em 'tempo', o
            # olho já viu a imagem mudando bem antes do ouvido ouvir o whoosh. Disparamos
            # o SFX de transição um pouco mais cedo (ANTECIPACAO_SFX_TRANSICAO) pra ele
            # coincidir com o MEIO do efeito visual em vez do final. Não se aplica ao
            # 'destaque' (mouse-click), que já está sincronizado corretamente.
            antecipacao = ANTECIPACAO_SFX_TRANSICAO if evento['tipo'] == 'transicao' else 0.0
            inicio_sfx = max(0.0, offset + evento['tempo'] - antecipacao)
            sfx_clip = AudioFileClip(arquivo).set_start(inicio_sfx).volumex(0.5)
            camadas.append(sfx_clip)
            aplicados += 1
            print(f"    🔊 SFX '{evento['tipo']}' em t={evento['tempo']:.2f}s → {os.path.basename(arquivo)}")
        except Exception as e:
            print(f"  ⚠️ Erro ao carregar SFX '{arquivo}': {e}")

    if aplicados == 0:
        return audio_base

    print(f"  🔊 {aplicados} evento(s) de SFX aplicado(s)")
    return CompositeAudioClip(camadas)


def criar_video_curto(audio_path, roteiro, lista_clipes, output_file, duracao_narracao,
                       clips_legenda=None, clips_destaque=None, eventos_sfx=None):
    """
    Vertical (short). Estrutura de tempo:
    [0s ───── música+vídeo ─────][3s narração começa ───...──][fim narração ── +5s música+vídeo][fade-out 2s]
    """
    print("📹 Criando short (Pexels)...")
    clips_legenda = clips_legenda or []
    clips_destaque = clips_destaque or []
    duracao_total = SEGUNDOS_LEAD_IN + duracao_narracao + SEGUNDOS_TAIL

    clips_video = _montar_clips_pexels(lista_clipes, 1080, 1920)
    if not clips_video:
        return None

    # BUGFIX: os itens de lista_clipes vêm com tempo relativo ao INÍCIO DA NARRAÇÃO
    # (0s = primeira palavra falada), mas a narração de verdade só começa em
    # SEGUNDOS_LEAD_IN dentro do vídeo final. Sem este deslocamento, cada troca de
    # mídia (e o SFX de transição, que já soma esse offset corretamente em
    # aplicar_sfx) acontecia SEGUNDOS_LEAD_IN segundos ANTES do corte visual real —
    # por isso o woosh soava "no meio da mídia" em vez de na troca.
    clips_video = [c.set_start(c.start + SEGUNDOS_LEAD_IN) for c in clips_video]

    # O 1º clipe cobria [0, SEGUNDOS_LEAD_IN) antes deste deslocamento — era o
    # próprio "lead-in" visual antes da narração começar. Puxamos ele de volta pro
    # instante 0 (mesma técnica já usada abaixo pro ÚLTIMO clipe, que é esticado
    # pra cobrir o tail): mantém o vídeo cobrindo o início sem tela preta.
    primeiro = clips_video[0]
    if primeiro.start > 0:
        gap_inicial = primeiro.start
        clips_video[0] = primeiro.set_start(0).set_duration(primeiro.duration + gap_inicial)

    ultimo = clips_video[-1]
    cobertura = ultimo.start + ultimo.duration
    if cobertura < duracao_total:
        clips_video[-1] = ultimo.set_duration(ultimo.duration + (duracao_total - cobertura))

    video_base = CompositeVideoClip(
        clips_video + clips_legenda + clips_destaque, size=(1080, 1920)
    ).set_duration(duracao_total)
    video_base = video_base.fadeout(SEGUNDOS_FADEOUT)

    audio_narr = AudioFileClip(audio_path).set_start(SEGUNDOS_LEAD_IN)
    audio_com_sfx = aplicar_sfx(audio_narr, eventos_sfx or [], offset=SEGUNDOS_LEAD_IN)
    audio_final = _mixar_musica_fundo(audio_com_sfx, duracao_total, volume=0.06)
    audio_final = audio_final.audio_fadeout(SEGUNDOS_FADEOUT)

    video_final = video_base.set_audio(audio_final)
    video_final.write_videofile(output_file, fps=30, codec='libx264', audio_codec='aac',
                                 preset='medium', bitrate='8000k', threads=4)

    video_final.close()
    audio_narr.close()
    for c in clips_video:
        c.close()
    return output_file


def criar_video_longo(audio_path, roteiro, lista_clipes, output_file, duracao_narracao,
                       clips_legenda=None, clips_destaque=None, eventos_sfx=None):
    """Horizontal (long), com intro fixa de assets/intro/ ANTES do bloco lead-in/narração/tail.

    Usado pelos modos 'cadeia_completa'/'simples'. O modo 'capitulos_webdoc' usa
    criar_video_webdoc_capitulos() em vez desta função — a estrutura de posicionamento
    da vinheta e dos cards de capítulo é bem diferente (vinheta DEPOIS de uma abertura
    sem vinheta, cards em silêncio real) pra valer a pena forçar as duas dentro da
    mesma função.
    """
    print("📹 Criando vídeo longo (Pexels + intro)...")
    clips_legenda = clips_legenda or []
    clips_destaque = clips_destaque or []

    import glob
    intros = glob.glob(f'{ASSETS_DIR}/intro/*.mp4') + glob.glob(f'{ASSETS_DIR}/intro/*.mov')
    intro_duracao = 0.0
    intro_clip = None

    if intros:
        intro_path = random.choice(intros) if len(intros) > 1 else intros[0]
        print(f"  🎬 Intro: {os.path.basename(intro_path)}")
        intro_bruto = VideoFileClip(intro_path)
        intro_clip = intro_bruto.resize(height=1080)
        if intro_clip.w > 1920:
            intro_clip = intro_clip.crop(x_center=intro_clip.w / 2, width=1920, height=1080)
        elif intro_clip.w < 1920:
            intro_clip = intro_clip.resize(width=1920)
        if intro_clip.size != (1920, 1080):
            intro_clip = intro_clip.resize((1920, 1080))
        intro_clip = intro_clip.without_audio()
        intro_duracao = intro_clip.duration
    else:
        print("  ℹ️ Nenhuma intro encontrada em assets/intro/ — seguindo sem intro")

    duracao_bloco = SEGUNDOS_LEAD_IN + duracao_narracao + SEGUNDOS_TAIL
    duracao_total = intro_duracao + duracao_bloco

    clips_video = _montar_clips_pexels(lista_clipes, 1920, 1080)
    # BUGFIX real (era o culpado do woosh desalinhado): faltava somar SEGUNDOS_LEAD_IN
    # aqui — só a intro estava sendo compensada. lista_clipes vem em tempo relativo ao
    # início da narração, mas a narração só começa em intro_duracao + SEGUNDOS_LEAD_IN
    # no vídeo final (ver offset_narracao mais abaixo, usado pro áudio e pro SFX).
    # Sem os dois offsets aqui, cada corte de mídia acontecia SEGUNDOS_LEAD_IN segundos
    # adiantado em relação ao evento de SFX/narração correspondente.
    clips_video = [c.set_start(c.start + intro_duracao + SEGUNDOS_LEAD_IN) for c in clips_video]

    # O 1º clipe cobria [intro_duracao, intro_duracao + SEGUNDOS_LEAD_IN) antes deste
    # deslocamento — o lead-in visual logo após a intro. Puxamos ele de volta (mesma
    # técnica já usada abaixo pro ÚLTIMO clipe, esticado pra cobrir o tail).
    if clips_video:
        primeiro = clips_video[0]
        if primeiro.start > intro_duracao:
            gap_inicial = primeiro.start - intro_duracao
            clips_video[0] = primeiro.set_start(intro_duracao).set_duration(primeiro.duration + gap_inicial)

    # BUGFIX: clips_legenda e clips_destaque chegam do main() com o tempo já calculado
    # em cima da narração (offset = SEGUNDOS_LEAD_IN), mas sem saber a duração da intro
    # (que só existe aqui dentro). Sem este deslocamento, eles tocavam `intro_duracao`
    # segundos adiantados — exatamente a dessincronia relatada.
    clips_legenda = [c.set_start(c.start + intro_duracao) for c in clips_legenda]
    clips_destaque = [c.set_start(c.start + intro_duracao) for c in clips_destaque]

    if clips_video:
        ultimo = clips_video[-1]
        cobertura = ultimo.start + ultimo.duration
        if cobertura < duracao_total:
            clips_video[-1] = ultimo.set_duration(ultimo.duration + (duracao_total - cobertura))

    offset_narracao = intro_duracao + SEGUNDOS_LEAD_IN

    todos_os_clips = ([intro_clip] if intro_clip else []) + clips_video + clips_legenda + clips_destaque
    if not todos_os_clips:
        return None

    video_base = CompositeVideoClip(todos_os_clips, size=(1920, 1080)).set_duration(duracao_total)
    video_base = video_base.fadeout(SEGUNDOS_FADEOUT)

    audio_narr = AudioFileClip(audio_path).set_start(offset_narracao)
    audio_com_sfx = aplicar_sfx(audio_narr, eventos_sfx or [], offset=offset_narracao)
    audio_final = _mixar_musica_fundo(audio_com_sfx, duracao_total, volume=0.06)
    audio_final = audio_final.audio_fadeout(SEGUNDOS_FADEOUT)

    video_final = video_base.set_audio(audio_final)
    video_final.write_videofile(output_file, fps=24, codec='libx264', audio_codec='aac',
                                 preset='medium', bitrate='6000k', threads=4)

    video_final.close()
    audio_narr.close()
    for c in todos_os_clips:
        c.close()
    return output_file


def montar_audio_webdoc_capitulos(blocos_roteiro, audio_path, intro_duracao_vinheta,
                                   duracao_card_capitulo):
    """
    Gera o áudio do modo 'capitulos_webdoc' segmento por segmento (introdução, cada
    capítulo, desfecho) — CADA UM como uma chamada de TTS separada — e concatena tudo
    com SILÊNCIO REAL nos pontos de troca de capítulo, em vez de narração contínua:

    - depois da introdução: silêncio = intro_duracao_vinheta + duracao_card_capitulo
      (tempo pra vinheta tocar inteira + o card do capítulo 1 aparecer, os dois SEM
      narração por cima)
    - antes de cada capítulo seguinte (2, 3, ...) e antes do desfecho, se tiver título
      de capítulo: silêncio = duracao_card_capitulo (só o card, vinheta não repete)

    É esse silêncio de verdade no ÁUDIO — não um efeito visual sobreposto — que garante
    o card não ser narrado: durante o card, literalmente não há fala nenhuma gravada.

    Retorna audio_path (mesmo arquivo, sobrescrito com o áudio final concatenado).
    """
    segmentos = []
    atual = []
    for b in blocos_roteiro:
        if b.get('inicio_capitulo') and atual:
            segmentos.append(atual)
            atual = []
        atual.append(b)
    if atual:
        segmentos.append(atual)

    pasta = f'{ASSETS_DIR}/audio_segmentos'
    os.makedirs(pasta, exist_ok=True)

    print(f"  🎬 Gerando áudio em {len(segmentos)} segmento(s) (introdução/capítulos/desfecho)...")
    clips_segmento = []
    for i, grupo in enumerate(segmentos):
        texto_segmento = " ".join(aplicar_correcoes_pronuncia(b['texto']) for b in grupo)
        caminho_segmento = f'{pasta}/segmento_{i:02d}.mp3'
        print(f"    Segmento {i + 1}/{len(segmentos)} ({grupo[0]['bloco']})...")
        criar_audio(texto_segmento, caminho_segmento)
        clips_segmento.append(AudioFileClip(caminho_segmento))

    intercalados = []
    for i, clip in enumerate(clips_segmento):
        intercalados.append(clip)
        if i < len(clips_segmento) - 1:
            eh_apos_introducao = (i == 0)
            duracao_silencio = duracao_card_capitulo + (intro_duracao_vinheta if eh_apos_introducao else 0)
            intercalados.append(AudioClip(lambda t: 0 * t, duration=duracao_silencio, fps=44100))

    audio_final = concatenate_audioclips(intercalados)
    audio_final.write_audiofile(audio_path, logger=None)
    for c in clips_segmento:
        c.close()
    audio_final.close()

    print(f"  ✅ Áudio final montado: {AudioFileClip(audio_path).duration:.1f}s "
          f"(incluindo os silêncios de vinheta/card entre segmentos)")
    return audio_path


def criar_video_webdoc_capitulos(audio_path, blocos_com_tempo, lista_clipes, output_file,
                                  duracao_narracao, clips_legenda=None, clips_destaque=None,
                                  eventos_sfx=None):
    """
    Montagem do modo 'capitulos_webdoc': SEM vinheta no início — o vídeo já abre
    falando/mostrando conteúdo (a "introdução", ~1min), termina em fade-to-black de 2s,
    SÓ ENTÃO a vinheta do canal toca, seguida do card preto do capítulo 1 (mudo).
    Cada capítulo depois disso termina em fade-to-black de 2s → card do próximo
    capítulo (mudo) → capítulo seguinte — até o desfecho.

    Pré-requisito: audio_path já foi montado por montar_audio_webdoc_capitulos(), então
    duracao_narracao aqui já inclui os silêncios reais de vinheta/card — a duração
    total do vídeo é exatamente duracao_narracao, sem nenhuma soma extra de intro.
    """
    print("📹 Criando vídeo webdoc em capítulos (abertura → vinheta → capítulos)...")
    clips_legenda = clips_legenda or []
    clips_destaque = clips_destaque or []

    import glob
    intros = glob.glob(f'{ASSETS_DIR}/intro/*.mp4') + glob.glob(f'{ASSETS_DIR}/intro/*.mov')
    intro_clip_bruto, intro_duracao = None, 0.0
    if intros:
        intro_path = random.choice(intros) if len(intros) > 1 else intros[0]
        print(f"  🎬 Vinheta (após a abertura): {os.path.basename(intro_path)}")
        intro_clip_bruto = VideoFileClip(intro_path).resize(height=1080)
        if intro_clip_bruto.w > 1920:
            intro_clip_bruto = intro_clip_bruto.crop(x_center=intro_clip_bruto.w / 2, width=1920, height=1080)
        elif intro_clip_bruto.w < 1920:
            intro_clip_bruto = intro_clip_bruto.resize(width=1920)
        if intro_clip_bruto.size != (1920, 1080):
            intro_clip_bruto = intro_clip_bruto.resize((1920, 1080))
        intro_clip_bruto = intro_clip_bruto.without_audio()
        intro_duracao = intro_clip_bruto.duration
    else:
        print("  ℹ️ Nenhuma vinheta encontrada em assets/intro/ — seguindo sem vinheta")

    clips_video = _montar_clips_pexels(lista_clipes, 1920, 1080)
    # Aqui NÃO somamos intro_duracao/SEGUNDOS_LEAD_IN como em criar_video_longo — os
    # timestamps de blocos_com_tempo (e portanto de lista_clipes, clips_legenda,
    # clips_destaque, eventos_sfx) já são o tempo FINAL de verdade, porque o áudio foi
    # montado com os silêncios de vinheta/card já embutidos (ver
    # montar_audio_webdoc_capitulos) — o Whisper transcreveu esse áudio final, não um
    # áudio "cru" que precisasse de deslocamento depois.
    duracao_fade = 2.0
    duracao_card = float(config.get('duracao_card_capitulo', 2.2))

    overlays = []
    for idx in range(len(blocos_com_tempo) - 1):
        b_atual = blocos_com_tempo[idx]
        b_prox = blocos_com_tempo[idx + 1]
        if not b_prox.get('inicio_capitulo'):
            continue  # troca comum de mídia dentro do mesmo capítulo — sem card, sem fade especial

        eh_antes_do_capitulo_1 = (idx == 0)
        fim_conteudo = b_atual['fim']

        preto = ColorClip((1920, 1080), color=(0, 0, 0), duration=duracao_fade)
        preto = preto.fadein(duracao_fade).set_start(max(0, fim_conteudo - duracao_fade))
        overlays.append(preto)

        cursor = fim_conteudo
        if eh_antes_do_capitulo_1 and intro_clip_bruto:
            overlays.append(intro_clip_bruto.set_start(cursor))
            cursor += intro_duracao

        card = gerar_card_capitulo(b_prox.get('titulo_capitulo', ''), 1920, 1080, duracao=duracao_card)
        overlays.append(card.set_start(cursor))

    n_capitulos = len([b for b in blocos_com_tempo if b.get('inicio_capitulo')])
    if overlays:
        print(f"  📑 {n_capitulos} card(s) de capítulo + vinheta + fade-to-black inseridos")

    marcos_capitulos = [b['inicio'] for b in blocos_com_tempo if b.get('inicio_capitulo')]

    todos_os_clips = clips_video + overlays + clips_legenda + clips_destaque
    if not todos_os_clips:
        return None

    video_base = CompositeVideoClip(todos_os_clips, size=(1920, 1080)).set_duration(duracao_narracao)
    video_base = video_base.fadeout(SEGUNDOS_FADEOUT)

    audio_narr = AudioFileClip(audio_path)
    audio_com_sfx = aplicar_sfx(audio_narr, eventos_sfx or [], offset=0.0)
    audio_final = _mixar_musica_por_capitulo(audio_com_sfx, duracao_narracao, marcos_capitulos, volume=0.06)
    audio_final = audio_final.audio_fadeout(SEGUNDOS_FADEOUT)

    video_final = video_base.set_audio(audio_final)
    video_final.write_videofile(output_file, fps=24, codec='libx264', audio_codec='aac',
                                 preset='medium', bitrate='6000k', threads=4)

    video_final.close()
    audio_narr.close()
    for c in todos_os_clips:
        c.close()
    return output_file


_FONTE_TTF_CANDIDATOS = [
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def _carregar_fonte_pil(tamanho):
    # Prioridade 1: fonte customizada definida no config.json (ex: fonts/RoadRage-Regular.ttf)
    if FONTE_THUMBNAIL_ARQUIVO and os.path.exists(FONTE_THUMBNAIL_ARQUIVO):
        return ImageFont.truetype(FONTE_THUMBNAIL_ARQUIVO, tamanho)

    # Prioridade 2: fontes de sistema conhecidas (fallback pros canais que não têm fonte própria)
    for caminho in _FONTE_TTF_CANDIDATOS:
        if os.path.exists(caminho):
            return ImageFont.truetype(caminho, tamanho)

    print("  ⚠️ Nenhuma fonte TTF encontrada — usando fonte padrão do PIL (qualidade menor)")
    return ImageFont.load_default()


def _carregar_fonte_pil_destaque(tamanho):
    """Mesmo fallback do _carregar_fonte_pil, mas priorizando FONTE_DESTAQUE — usado
    pelo card de capítulo, que quer a MESMA fonte do texto de destaque (identidade
    visual consistente entre os dois elementos de texto mais chamativos do vídeo)."""
    if FONTE_DESTAQUE and os.path.exists(FONTE_DESTAQUE):
        return ImageFont.truetype(FONTE_DESTAQUE, tamanho)
    for caminho in _FONTE_TTF_CANDIDATOS:
        if os.path.exists(caminho):
            return ImageFont.truetype(caminho, tamanho)
    return ImageFont.load_default()


def gerar_card_capitulo(titulo, largura, altura, duracao=None):
    """
    Card preto de transição entre capítulos (formato webdoc tipo Elementar): título
    do capítulo centralizado em branco sobre fundo preto, com fade in/out suave.

    Sincronismo: esse clipe é posicionado (por quem chama, em criar_video_longo) EXATAMENTE
    no timestamp de início do bloco de capítulo correspondente — que é também o instante
    em que o narrador começa a FALAR esse mesmo título (ver gerar_prosa_capitulos no
    roteiro_engine.py, que prefixa o texto do bloco com o título falado). Por isso a
    duração default é curta (pensada pra cobrir só o tempo de falar um título de 3-6
    palavras, não o capítulo inteiro) — ajustável via config.json ('duracao_card_capitulo').
    """
    from moviepy.editor import ImageClip

    duracao = duracao if duracao is not None else float(config.get('duracao_card_capitulo', 2.2))
    largura_texto = int(largura * 0.8)

    img = Image.new('RGB', (largura, altura), color=(0, 0, 0))
    draw = ImageDraw.Draw(img)

    titulo_upper = titulo.upper()
    fontsize = int(largura / 14)
    fonte = _carregar_fonte_pil_destaque(fontsize)
    for _tentativa in range(6):
        fonte = _carregar_fonte_pil_destaque(fontsize)
        bbox = draw.textbbox((0, 0), titulo_upper, font=fonte)
        largura_medida = (bbox[2] - bbox[0]) * 1.08
        if largura_medida <= largura_texto or fontsize <= 30:
            break
        fontsize = max(30, int(fontsize * largura_texto / largura_medida))

    bbox = draw.textbbox((0, 0), titulo_upper, font=fonte)
    x = (largura - (bbox[2] - bbox[0])) / 2 - bbox[0]
    y = (altura - (bbox[3] - bbox[1])) / 2 - bbox[1]
    draw.text((x, y), titulo_upper, font=fonte, fill=(255, 255, 255))

    caminho_temp = os.path.join(ASSETS_DIR, f'_card_capitulo_{abs(hash(titulo_upper)) % 100000}.png')
    img.save(caminho_temp)

    fade = min(0.4, duracao / 4)
    clip = ImageClip(caminho_temp).set_duration(duracao).fadein(fade).fadeout(fade)
    return clip


def pesquisar_foto_pexels(termo):
    """Busca fotos (não vídeos) no Pexels — usada só para a thumbnail."""
    headers = {"Authorization": PEXELS_API_KEY}
    params = {"query": termo, "orientation": "landscape", "per_page": 15}
    resp = requests.get("https://api.pexels.com/v1/search", headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get('photos', [])


def gerar_texto_thumbnail(titulo, tema):
    """
    Gera um texto curto e impactante pra thumbnail (em MAIÚSCULAS), no estilo de canais
    motivacionais virais — um resumo/gancho direto do título, não o título reescrito.
    Se falhar, usa as 3 primeiras palavras do título como texto de segurança.
    """
    prompt = f"""Baseado neste vídeo de {CONTEXTO_NICHO}, crie um texto CURTO para
thumbnail de YouTube, em MAIÚSCULAS, no estilo de canais motivacionais virais.

Exemplos de título -> possíveis textos de thumbnail (estilo de referência — escolha UMA ideia
central, não junte as duas; os exemplos abaixo estão em português só como referência de padrão,
mas sua resposta deve ser em {IDIOMA_CONTEUDO}):
"E se você parasse de se preocupar com o amanhã? Descubra a paz que restaura a alma"
-> "PAZ NA ALMA"  (ou, alternativamente: "PREOCUPAÇÕES?")

"Uma nova chance toda manhã: A bondade de Deus em sua vida"
-> "UMA NOVA CHANCE"  (ou, alternativamente: "A BONDADE DE DEUS")

Outros exemplos do estilo geral: "NUNCA SE DEFENDA", "INTELIGÊNCIA SOMBRIA", "CONFIANÇA SOMBRIA",
"NUNCA SE EXPLIQUE".

TEMA: {tema}
TÍTULO DO VÍDEO: {titulo}

REGRAS OBRIGATÓRIAS:
- Escreva em {IDIOMA_CONTEUDO}, TODO EM MAIÚSCULAS
- Entre 2 e 4 palavras — NUNCA junte duas ideias diferentes na mesma frase
- Escolha a ideia/palavra mais forte e impactante do título, não tente resumir tudo
- Impactante, intrigante, desperta curiosidade — não é uma frase explicativa
- É um resumo/gancho, NÃO é o título do vídeo reescrito
- Sem ponto final (pode ter "?"), sem aspas

Retorne APENAS o texto da thumbnail, nada mais."""

    try:
        resposta = _gemini_generate(prompt)
        texto = resposta.text.strip().strip('"').strip("'").upper()
        if texto and len(texto.split()) <= 5:
            return texto
    except Exception as e:
        print(f"  ⚠️ Erro ao gerar texto da thumbnail: {e}")

    return " ".join(titulo.split()[:3]).upper()


def gerar_imagem_agnes(termo, output_path):
    """
    Gera a imagem de fundo da thumbnail via Agnes AI (gratuito). Se falhar por qualquer motivo
    (sem chave configurada, erro de rede, mudança na API), retorna None — o chamador cai pro
    fallback do Pexels automaticamente.
    """
    if not AGNES_API_KEY:
        return None

    try:
        headers = {
            "Authorization": f"Bearer {AGNES_API_KEY}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": AGNES_IMAGE_MODEL,
            "prompt": f"{termo}, cinematic, high quality, widescreen composition",
            "size": "1024x576"
        }
        resp = requests.post(AGNES_URL, headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
        dados = resp.json()
        url_imagem = dados['data'][0]['url']

        img_resp = requests.get(url_imagem, timeout=30)
        img_resp.raise_for_status()
        with open(output_path, 'wb') as f:
            f.write(img_resp.content)

        print("  ✅ Imagem gerada via Agnes AI")
        return output_path
    except Exception as e:
        print(f"  ⚠️ Agnes AI falhou ({e}) — usando fallback do Pexels")
        return None


def _animar_imagem_agnes(url_imagem_publica, output_path, prompt_movimento=None,
                          timeout_total=180, intervalo_polling=6):
    """
    Anima uma imagem (Image-to-Video) via Agnes AI. Assíncrono, conforme documentação
    oficial (wiki.agnes-ai.com): POST cria a tarefa (devolve video_id — campo
    recomendado pela doc pra consultar o resultado, em vez de task_id), depois GET faz
    polling em /v1/videos/{video_id} até status='completed', e o MP4 final vem em
    metadata.url dentro da resposta do GET.

    IMPORTANTE: url_imagem_publica precisa ser usada IMEDIATAMENTE após ser gerada
    (ela expira) — por isso essa função nunca é chamada isolada, só encadeada dentro de
    gerar_midia_agnes(), logo depois do POST de geração de imagem.
    """
    headers = {"Authorization": f"Bearer {AGNES_API_KEY}", "Content-Type": "application/json"}
    prompt_movimento = prompt_movimento or "Subtle, slow, natural camera drift and ambient motion, cinematic"
    try:
        payload = {"model": AGNES_VIDEO_MODEL, "prompt": prompt_movimento, "image_url": url_imagem_publica}
        resp = requests.post(AGNES_VIDEO_URL, headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
        dados = resp.json()
        video_id = dados.get('video_id') or dados.get('task_id')
        if not video_id:
            print("    ⚠️ Agnes vídeo: resposta sem video_id/task_id")
            return None

        print(f"    ⏳ Agnes vídeo: tarefa {video_id} criada, aguardando processamento...")
        decorrido = 0
        while decorrido < timeout_total:
            time.sleep(intervalo_polling)
            decorrido += intervalo_polling
            status_resp = requests.get(f"{AGNES_VIDEO_URL}/{video_id}", headers=headers, timeout=30)
            status_resp.raise_for_status()
            status_dados = status_resp.json()
            status = status_dados.get('status')

            if status == 'completed':
                url_video = (status_dados.get('metadata') or {}).get('url')
                if not url_video:
                    print("    ⚠️ Agnes vídeo: status completed mas sem metadata.url")
                    return None
                video_resp = requests.get(url_video, timeout=60)
                video_resp.raise_for_status()
                with open(output_path, 'wb') as f:
                    f.write(video_resp.content)
                print(f"    🎬 Vídeo animado via Agnes AI ({decorrido}s de processamento)")
                return output_path
            if status in ('failed', 'error'):
                print(f"    ⚠️ Agnes vídeo: tarefa retornou status='{status}'")
                return None
            # qualquer outro status (processing/pending/queued...) = continua o polling

        print(f"    ⚠️ Agnes vídeo: timeout de {timeout_total}s esperando processamento")
        return None
    except Exception as e:
        print(f"    ⚠️ Agnes vídeo falhou ({e})")
        return None


def gerar_midia_agnes(termo, output_dir, tentar_video=False, prompt_movimento=None):
    """
    Gera mídia via Agnes AI pro pool de B-roll: sempre gera a imagem primeiro; se
    tentar_video=True (config 'agnes_gerar_video' — desligado por padrão, animação
    custa mais tempo/processamento que imagem estática), encadeia a animação usando a
    MESMA url pública da imagem recém-gerada, antes dela expirar. Se a animação
    falhar ou estourar o timeout de polling, cai pra devolver a imagem estática mesmo
    — nunca fica sem nada só porque o vídeo não ficou pronto a tempo.

    Retorna (caminho_arquivo, é_video) — é_video=True só quando realmente veio um MP4
    animado; (None, False) se tudo falhar (sem AGNES_API_KEY, rede fora do ar, etc.).
    """
    if not AGNES_API_KEY:
        return None, False

    os.makedirs(output_dir, exist_ok=True)
    sufixo = abs(hash(termo + str(random.random()))) % 100000
    caminho_imagem = os.path.join(output_dir, f"agnes_{sufixo}.jpg")

    try:
        headers = {"Authorization": f"Bearer {AGNES_API_KEY}", "Content-Type": "application/json"}
        payload_img = {
            "model": AGNES_IMAGE_MODEL,
            "prompt": f"{termo}, cinematic, high quality, widescreen composition",
            "size": "1024x576"
        }
        resp = requests.post(AGNES_URL, headers=headers, json=payload_img, timeout=60)
        resp.raise_for_status()
        url_imagem_publica = resp.json()['data'][0]['url']

        img_resp = requests.get(url_imagem_publica, timeout=30)
        img_resp.raise_for_status()
        with open(caminho_imagem, 'wb') as f:
            f.write(img_resp.content)
        print(f"    ✅ Imagem gerada via Agnes AI: \"{termo}\"")
    except Exception as e:
        print(f"    ⚠️ Agnes AI (imagem) falhou ({e})")
        return None, False

    if not tentar_video:
        return caminho_imagem, False

    caminho_video = os.path.join(output_dir, f"agnes_{sufixo}.mp4")
    resultado_video = _animar_imagem_agnes(url_imagem_publica, caminho_video, prompt_movimento)
    if resultado_video:
        return resultado_video, True

    print("    ℹ️ Agnes AI: animação não ficou pronta — usando a imagem estática mesmo")
    return caminho_imagem, False


def _cortar_para_ratio(img, largura, altura):
    """Corta uma imagem PIL pro ratio exato largura:altura, cobrindo o quadro sem distorcer."""
    alvo_ratio = largura / altura
    img_ratio = img.width / img.height
    if img_ratio > alvo_ratio:
        novo_w = int(img.height * alvo_ratio)
        corte = (img.width - novo_w) // 2
        img = img.crop((corte, 0, corte + novo_w, img.height))
    else:
        novo_h = int(img.width / alvo_ratio)
        corte = (img.height - novo_h) // 2
        img = img.crop((0, corte, img.width, corte + novo_h))
    return img.resize((largura, altura))


def _gerar_thumbnail_pexels_agnes(titulo, termo, output_path, largura, altura):
    """
    Fundo via Agnes AI (fallback Pexels) + título em 2-3 linhas na LATERAL (esquerda
    ou direita, sorteado por vídeo — não sempre o mesmo lado), SEM faixa sólida atrás:
    cada letra ganha um contorno preto grosso, que garante legibilidade sobre qualquer
    fundo sem precisar cobrir a imagem com uma barra. Mantém o esquema de duas cores
    (branco + amarelo no "gancho" final) do estilo de referência do canal.
    """
    caminho_bruto = f"{ASSETS_DIR}/thumb_bruta.jpg"

    resultado_agnes = gerar_imagem_agnes(termo, caminho_bruto)

    if not resultado_agnes:
        fotos = pesquisar_foto_pexels(termo)
        if not fotos:
            print("  ⚠️ Nenhuma foto encontrada no Pexels para thumbnail")
            return None

        foto = random.choice(fotos[:5])
        src = foto.get('src', {})
        url_imagem = src.get('landscape') or src.get('large') or src.get('original')
        if not url_imagem:
            return None

        resp = requests.get(url_imagem, timeout=30)
        resp.raise_for_status()
        with open(caminho_bruto, 'wb') as f:
            f.write(resp.content)

    img = Image.open(caminho_bruto).convert('RGB')
    img = _cortar_para_ratio(img, largura, altura)
    draw = ImageDraw.Draw(img)

    lado = random.choice(['esquerda', 'direita'])
    largura_texto_max = int(largura * 0.46)
    margem = int(largura * 0.05)

    palavras = titulo.upper().split()
    # última 1-2 palavras = o "gancho" em amarelo (ex: "A INFLAÇÃO" branco / "INVISÍVEL?"
    # amarelo, "A MÁFIA DOS" branco / "PERFUMES?" amarelo — mesmo padrão dos exemplos)
    n_destaque = 2 if len(palavras) > 4 else 1
    palavras_base = palavras[:-n_destaque] if len(palavras) > n_destaque else []
    palavras_destaque = palavras[-n_destaque:] if palavras else palavras

    def _quebrar(lista_palavras, fonte):
        linhas, atual = [], []
        for p in lista_palavras:
            teste = " ".join(atual + [p])
            bbox = draw.textbbox((0, 0), teste, font=fonte)
            if bbox[2] - bbox[0] <= largura_texto_max or not atual:
                atual.append(p)
            else:
                linhas.append(" ".join(atual))
                atual = [p]
        if atual:
            linhas.append(" ".join(atual))
        return linhas

    tamanho_fonte = int(altura * 0.15)
    tamanho_minimo = int(altura * 0.05)
    fonte, linhas_base, linhas_destaque = None, [], []
    while tamanho_fonte >= tamanho_minimo:
        fonte = _carregar_fonte_pil(tamanho_fonte)
        linhas_base = _quebrar(palavras_base, fonte) if palavras_base else []
        linhas_destaque = _quebrar(palavras_destaque, fonte)
        if len(linhas_base) + len(linhas_destaque) <= 3:  # no máx. 3 linhas no total
            break
        tamanho_fonte = int(tamanho_fonte * 0.92)

    todas_linhas = [(l, (255, 255, 255)) for l in linhas_base] + [(l, (255, 209, 0)) for l in linhas_destaque]
    espacamento_linha = 1.15
    alturas_linha = [draw.textbbox((0, 0), l, font=fonte)[3] - draw.textbbox((0, 0), l, font=fonte)[1]
                      for l, _ in todas_linhas]
    altura_total = sum(int(h * espacamento_linha) for h in alturas_linha)
    y = int(altura * 0.5 - altura_total / 2)

    contorno = max(3, tamanho_fonte // 16)
    for linha, cor in todas_linhas:
        bbox = draw.textbbox((0, 0), linha, font=fonte)
        largura_linha = bbox[2] - bbox[0]
        x = margem if lado == 'esquerda' else (largura - margem - largura_linha)
        # contorno preto grosso em vez de faixa sólida — legível sobre qualquer fundo
        # sem precisar cobrir parte da imagem gerada
        for dx in range(-contorno, contorno + 1, max(1, contorno // 2)):
            for dy in range(-contorno, contorno + 1, max(1, contorno // 2)):
                if dx or dy:
                    draw.text((x + dx, y + dy), linha, font=fonte, fill=(0, 0, 0))
        draw.text((x, y), linha, font=fonte, fill=cor)
        y += int((bbox[3] - bbox[1]) * espacamento_linha)

    img.save(output_path, quality=90)
    return output_path


# ============================================================
# Thumbnail — método "pool local" (imagens prontas + rotatividade)
# ============================================================

THUMBS_LOG_FILE = 'thumbs_usadas.json'


def _carregar_thumbs_usadas():
    if os.path.exists(THUMBS_LOG_FILE):
        try:
            with open(THUMBS_LOG_FILE, 'r', encoding='utf-8') as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def _salvar_thumb_usada(nome_arquivo):
    usadas = _carregar_thumbs_usadas()
    usadas.add(nome_arquivo)
    with open(THUMBS_LOG_FILE, 'w', encoding='utf-8') as f:
        json.dump(sorted(usadas), f, indent=2, ensure_ascii=False)


def escolher_imagem_thumbnail_pool():
    """
    Escolhe uma imagem da pasta de thumbnails prontas (config['pasta_thumbs'], padrão 'thumbs/'),
    sem repetir nenhuma até que todas já tenham sido usadas — aí o ciclo reinicia.
    """
    pasta = config.get('pasta_thumbs', 'thumbs')
    if not os.path.isdir(pasta):
        print(f"  ⚠️ Pasta de thumbnails '{pasta}' não encontrada")
        return None

    todas = [f for f in os.listdir(pasta) if f.lower().endswith(('.jpg', '.jpeg', '.png'))]
    if not todas:
        print(f"  ⚠️ Nenhuma imagem encontrada em '{pasta}'")
        return None

    usadas = _carregar_thumbs_usadas()
    disponiveis = [f for f in todas if f not in usadas]

    if not disponiveis:
        print("  🔄 Todas as thumbnails já foram usadas — reiniciando o ciclo de rotatividade")
        disponiveis = todas

    escolhida = random.choice(disponiveis)
    return os.path.join(pasta, escolhida), escolhida


def _quebrar_em_linhas(palavras, palavras_por_linha=2):
    """Agrupa palavras em linhas de até N palavras cada (padrão: 2 por linha)."""
    return [' '.join(palavras[i:i + palavras_por_linha]) for i in range(0, len(palavras), palavras_por_linha)]


def _gerar_thumbnail_pool_local(texto_thumb, output_path, largura, altura):
    """
    Método alternativo: usa uma imagem pronta de config['pasta_thumbs'] (metade esquerda com
    cenário/personagem, metade direita já escurecida no design da própria imagem), sem faixa
    preta — o texto (2-3 linhas, 2 cores) é desenhado direto na metade direita.
    """
    resultado = escolher_imagem_thumbnail_pool()
    if not resultado:
        return None
    caminho_imagem, nome_arquivo = resultado

    img = Image.open(caminho_imagem).convert('RGB')
    img = _cortar_para_ratio(img, largura, altura)
    draw = ImageDraw.Draw(img)

    palavras = texto_thumb.split()
    linhas = _quebrar_em_linhas(palavras, palavras_por_linha=2)

    # Região de texto: metade direita da imagem, com margem
    x_inicio = int(largura * 0.52)
    largura_disponivel = int(largura * 0.94) - x_inicio

    tamanho_fonte = int(altura * 0.16)
    tamanho_minimo = int(altura * 0.06)
    espacamento = int(altura * 0.02)

    def _medir(linhas_teste, fonte):
        alturas, larguras = [], []
        for linha in linhas_teste:
            bbox = draw.textbbox((0, 0), linha, font=fonte)
            larguras.append(bbox[2] - bbox[0])
            alturas.append(bbox[3] - bbox[1])
        return larguras, alturas

    # Auto-ajuste: reduz a fonte até todas as linhas caberem na largura disponível;
    # se mesmo no tamanho mínimo não couber, quebra em mais linhas (1 palavra cada)
    fonte = _carregar_fonte_pil(tamanho_fonte)
    larguras, alturas = _medir(linhas, fonte)

    while max(larguras) > largura_disponivel and tamanho_fonte > tamanho_minimo:
        tamanho_fonte = int(tamanho_fonte * 0.9)
        fonte = _carregar_fonte_pil(tamanho_fonte)
        larguras, alturas = _medir(linhas, fonte)

    if max(larguras) > largura_disponivel and len(palavras) > len(linhas):
        # Ainda não coube — quebra em 1 palavra por linha e tenta de novo
        linhas = _quebrar_em_linhas(palavras, palavras_por_linha=1)
        tamanho_fonte = int(altura * 0.16)
        fonte = _carregar_fonte_pil(tamanho_fonte)
        larguras, alturas = _medir(linhas, fonte)
        while max(larguras) > largura_disponivel and tamanho_fonte > tamanho_minimo:
            tamanho_fonte = int(tamanho_fonte * 0.9)
            fonte = _carregar_fonte_pil(tamanho_fonte)
            larguras, alturas = _medir(linhas, fonte)

    altura_bloco = sum(alturas) + espacamento * (len(linhas) - 1)
    y = (altura - altura_bloco) // 2
    cores = [(255, 215, 0), (255, 255, 255)]  # alterna dourado/branco por linha

    for i, linha in enumerate(linhas):
        x = x_inicio + (largura_disponivel - larguras[i]) // 2
        draw.text((x, y), linha, font=fonte, fill=cores[i % len(cores)])
        y += alturas[i] + espacamento

    img.save(output_path, quality=90)
    _salvar_thumb_usada(nome_arquivo)
    print(f"  ✅ Thumbnail gerada a partir de '{nome_arquivo}' (pool local)")
    return output_path


def gerar_thumbnail(titulo, termo, output_path, largura=1280, altura=720):
    """
    Gera a thumbnail final. 'titulo' aqui já é o texto curto gerado por gerar_texto_thumbnail()
    (quem chama essa função já faz essa conversão antes). O método usado depende de
    config['metodo_thumbnail']:
    - "pexels_agnes" (padrão): imagem via Agnes AI/Pexels + faixa preta no topo
    - "pool_local": imagem pronta de config['pasta_thumbs'], com rotatividade, texto na metade
      direita (pensado pra imagens já com metade direita escurecida no próprio design)
    Se qualquer etapa falhar, retorna None — o vídeo publica normalmente, só sem thumbnail
    customizada (o YouTube usa uma automática).
    """
    metodo = config.get('metodo_thumbnail', 'pexels_agnes')

    try:
        if metodo == 'pool_local':
            return _gerar_thumbnail_pool_local(titulo, output_path, largura, altura)
        else:
            return _gerar_thumbnail_pexels_agnes(titulo, termo, output_path, largura, altura)
    except Exception as e:
        print(f"  ⚠️ Erro ao gerar thumbnail: {e} — publicando sem thumbnail customizada")
        return None


def fazer_upload_youtube(video_path, titulo, descricao, tags, thumbnail_path=None):
    # BUGFIX (troca de esquema de credencial): antes um único secret YOUTUBE_CREDENTIALS
    # trazia o JSON inteiro (token, refresh_token, client_id, client_secret, scopes...)
    # gerado por Credentials.to_json(). Trocamos pra 3 secrets separados — mais simples
    # de gerar/rotacionar (não depende de guardar o JSON completo em lugar nenhum, só o
    # client_id/client_secret do app OAuth + o refresh_token dessa conta específica).
    # Sem 'token' (access token) aqui de propósito — a lib renova ele sozinha usando o
    # refresh_token, e forçamos isso explicitamente com .refresh() logo abaixo, pra
    # falhar rápido e com erro claro se o refresh_token estiver errado/revogado, em vez
    # de um erro genérico e confuso lá na hora do upload em si.
    if not (YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET and YOUTUBE_REFRESH_TOKEN):
        raise RuntimeError(
            "Credenciais do YouTube incompletas — defina YOUTUBE_CLIENT_ID, "
            "YOUTUBE_CLIENT_SECRET e YOUTUBE_REFRESH_TOKEN nos secrets do repositório."
        )

    credentials = Credentials(
        token=None,
        refresh_token=YOUTUBE_REFRESH_TOKEN,
        client_id=YOUTUBE_CLIENT_ID,
        client_secret=YOUTUBE_CLIENT_SECRET,
        token_uri='https://oauth2.googleapis.com/token',
    )
    credentials.refresh(GoogleAuthRequest())

    youtube = build('youtube', 'v3', credentials=credentials)

    body = {
        'snippet': {'title': titulo, 'description': descricao, 'tags': tags, 'categoryId': '22'},
        'status': {'privacyStatus': 'public', 'selfDeclaredMadeForKids': False}
    }

    media = MediaFileUpload(video_path, resumable=True)
    request = youtube.videos().insert(part='snippet,status', body=body, media_body=media)
    response = request.execute()
    video_id = response['id']

    if thumbnail_path and os.path.exists(thumbnail_path):
        try:
            youtube.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(thumbnail_path)).execute()
        except Exception as e:
            print(f"❌ Erro thumbnail: {e}")

    return video_id


def main():
    print(f"{'📱' if VIDEO_TYPE == 'short' else '🎬'} Iniciando ({VIDEO_TYPE})...")
    os.makedirs(VIDEOS_DIR, exist_ok=True)
    os.makedirs(ASSETS_DIR, exist_ok=True)

    tema = escolher_tema_reflexao()

    print("✍️ Gerando roteiro (cadeia: tese → estrutura → escrita → crítica → título/descrição)...")
    pacote_roteiro = gerar_pacote_roteiro(
        tema=tema,
        contexto_nicho=CONTEXTO_NICHO,
        idioma_conteudo=IDIOMA_CONTEUDO,
        instrucao_extra=INSTRUCAO_EXTRA_ROTEIRO,
        documento_estilo=config.get('documento_estilo', []),
        tipo_video=VIDEO_TYPE,
        gemini_generate_fn=_gemini_generate,
        modo_roteiro=config.get('modo_roteiro', 'cadeia_completa'),
        num_capitulos=config.get('num_capitulos_webdoc', 3),
    )
    roteiro = pacote_roteiro['roteiro_texto']
    titulo_video = pacote_roteiro['titulo']
    blocos_roteiro = pacote_roteiro['roteiro_blocos']  # reservado para Fase 2 (B-roll por bloco)
    print(f"🎯 Título: {titulo_video}")
    if pacote_roteiro.get('tese'):
        print(f"🧭 Tese: {pacote_roteiro['tese']}")
    # revisar_roteiro() antigo não é mais necessário: o Estágio 4 da cadeia (crítica
    # adversarial) já cobre correção de qualidade; ortografia grosseira é rara no Gemini
    # e o roteiro já passa por _extrair_json, que falharia com texto muito malformado.

    audio_path = f'{ASSETS_DIR}/audio.mp3'
    eh_webdoc_capitulos = (pacote_roteiro.get('modo') == 'capitulos_webdoc')

    if eh_webdoc_capitulos:
        # Modo capítulos: cada segmento (introdução/capítulo/desfecho) é gerado como
        # áudio SEPARADO e concatenado com SILÊNCIO REAL nos pontos de troca de
        # capítulo — é isso que garante o card de transição não ser narrado (ver
        # docstring de montar_audio_webdoc_capitulos). intro_duracao_vinheta precisa
        # ser conhecida ANTES de montar o áudio, pra reservar o silêncio certo.
        import glob
        intros_probe = glob.glob(f'{ASSETS_DIR}/intro/*.mp4') + glob.glob(f'{ASSETS_DIR}/intro/*.mov')
        intro_duracao_vinheta = 0.0
        if intros_probe:
            _clip_probe = VideoFileClip(intros_probe[0])
            intro_duracao_vinheta = _clip_probe.duration
            _clip_probe.close()
        duracao_card_capitulo = float(config.get('duracao_card_capitulo', 2.2))

        montar_audio_webdoc_capitulos(blocos_roteiro, audio_path, intro_duracao_vinheta,
                                       duracao_card_capitulo)
    else:
        texto_falado = aplicar_correcoes_pronuncia(roteiro)
        # A pausa entre frases (config 'duracao_pausa_frases_ms') agora é aplicada DENTRO
        # de criar_audio_fishaudio, entre os arquivos de frase já separados — não como
        # pós-processamento cego em cima do áudio pronto (ver bugfix no próprio arquivo).
        criar_audio(texto_falado, audio_path)

    audio_clip = AudioFileClip(audio_path)
    duracao_narracao = audio_clip.duration
    audio_clip.close()
    print(f"⏱️ {duracao_narracao:.1f}s de narração")

    # Fase 2: transcreve UMA VEZ aqui (cedo) e reaproveita em tudo que segue —
    # mapeamento de bloco, legenda comum e destaque visual não retranscrevem.
    print("🧠 Transcrevendo narração (relógio mestre para B-roll/legenda/destaque/SFX)...")
    try:
        palavras_tempo = transcrever_palavras_com_timestamps(audio_path)
    except Exception as e:
        print(f"⚠️ Falha na transcrição ({e}) — seguindo sem timestamps de palavra")
        palavras_tempo = []

    orientacao = 'portrait' if VIDEO_TYPE == 'short' else 'landscape'
    duracao_bloco_video = SEGUNDOS_LEAD_IN + duracao_narracao + SEGUNDOS_TAIL

    if palavras_tempo:
        blocos_com_tempo = mapear_tempos_para_blocos(blocos_roteiro, palavras_tempo)

        termos_validados = config.get('termos_pesquisa_validados', [])
        print("🔍 Escolhendo termo de busca por bloco (B-roll casado com o que está sendo dito)...")
        termos_por_bloco = escolher_termos_por_bloco(tema, blocos_com_tempo, termos_validados, _gemini_generate)
        for bloco, termo_bloco in zip(blocos_com_tempo, termos_por_bloco):
            bloco['termo'] = termo_bloco

        # Webdoc, opt-in via config.json ('usar_prints_noticia': true) — inerte pro canal
        # atual, que não tem essa chave. Ver producao_visual.decidir_prints_de_noticia.
        blocos_com_tempo = decidir_prints_de_noticia(
            blocos_com_tempo, _gemini_generate,
            usar_prints_noticia=config.get('usar_prints_noticia', False)
        )

        lista_clipes = baixar_clipes_por_bloco(blocos_com_tempo, orientacao)

        print("✨ Escolhendo palavras de destaque...")
        destaques_por_bloco = escolher_palavras_destaque(blocos_com_tempo, _gemini_generate)
        destaques_resolvidos = resolver_destaques_com_tempo(
            roteiro, palavras_tempo, blocos_com_tempo, destaques_por_bloco
        )
        eventos_sfx = construir_timeline_sfx(blocos_com_tempo, destaques_resolvidos)

        largura_legenda = 1080 if VIDEO_TYPE == 'short' else 1920
        altura_legenda = 1920 if VIDEO_TYPE == 'short' else 1080
        offset_legenda = SEGUNDOS_LEAD_IN  # NÃO inclui intro — quem soma intro_duracao é criar_video_longo
        # Formato webdoc costuma preferir só a palavra de destaque na tela, sem legenda
        # corrida por baixo (visual mais limpo, menos "poluído") — config.json →
        # 'usar_legenda': false. Destaque continua sempre ativo (não tem toggle próprio
        # porque é o elemento visual mais forte do formato, não teria por que desligar).
        if config.get('usar_legenda', True):
            clips_legenda = gerar_clips_legenda(roteiro, palavras_tempo, largura_legenda, altura_legenda,
                                                 offset=offset_legenda)
        else:
            clips_legenda = []
        clips_destaque = gerar_clips_destaque(roteiro, palavras_tempo, destaques_resolvidos,
                                               largura_legenda, altura_legenda, offset=offset_legenda)
        termo = termos_por_bloco[0] if termos_por_bloco else ''  # usado só na busca de foto da thumbnail
    else:
        # sem timestamps não dá pra fazer nada da Fase 2 — cai pro comportamento antigo
        # (1 termo pro vídeo inteiro, sem legenda/destaque/SFX sincronizados)
        termo = escolher_termo_pesquisa(tema, roteiro)
        lista_clipes = baixar_clipes_pexels(termo, orientacao, duracao_bloco_video)
        clips_legenda, clips_destaque, eventos_sfx = [], [], []
        blocos_com_tempo = []  # sem isso, criar_video_longo(blocos_com_tempo=...) abaixo quebraria com NameError

    if not lista_clipes:
        print("❌ Nenhum clipe baixado — abortando este ciclo.")
        return

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    video_path = f'{VIDEOS_DIR}/{VIDEO_TYPE}_{timestamp}.mp4'

    print("🎥 Montando vídeo...")
    try:
        if VIDEO_TYPE == 'short':
            resultado = criar_video_curto(audio_path, roteiro, lista_clipes, video_path, duracao_narracao,
                                           clips_legenda=clips_legenda, clips_destaque=clips_destaque,
                                           eventos_sfx=eventos_sfx)
        elif eh_webdoc_capitulos:
            resultado = criar_video_webdoc_capitulos(audio_path, blocos_com_tempo, lista_clipes, video_path,
                                                       duracao_narracao, clips_legenda=clips_legenda,
                                                       clips_destaque=clips_destaque, eventos_sfx=eventos_sfx)
        else:
            resultado = criar_video_longo(audio_path, roteiro, lista_clipes, video_path, duracao_narracao,
                                           clips_legenda=clips_legenda, clips_destaque=clips_destaque,
                                           eventos_sfx=eventos_sfx)

        if not resultado:
            print("❌ Erro ao criar vídeo")
            return
        print("✅ Vídeo criado!")
    except Exception as e:
        print(f"❌ Erro: {e}")
        import traceback
        traceback.print_exc()
        return

    titulo = titulo_video[:60] if len(titulo_video) <= 60 else titulo_video[:57] + '...'
    if VIDEO_TYPE == 'short':
        titulo += ' #shorts'

    texto_inscricao = config.get('texto_inscricao', '🔔 Inscreva-se para reflexões diárias!')
    hashtag_conteudo = config.get('hashtag_conteudo', 'reflexao')
    descricao_pacote = pacote_roteiro.get('descricao') or {}
    corpo_descricao = (
        f"{descricao_pacote.get('abertura_seo', '')}\n\n{descricao_pacote.get('corpo', '')}"
        if descricao_pacote.get('corpo') else roteiro[:300] + '...'
    )
    descricao = corpo_descricao + f'\n\n{texto_inscricao}\n#' + \
                ('shorts' if VIDEO_TYPE == 'short' else hashtag_conteudo)
    tags = config.get('tags_padrao', ['reflexao crista', 'motivacional', 'fe', 'inspiracao'])
    if VIDEO_TYPE == 'short':
        tags.append('shorts')

    _salvar_tema_usado(tema)

    if PULAR_UPLOAD:
        print(f"\n⏭️ PULAR_UPLOAD ativo — vídeo NÃO será publicado. Disponível em: {video_path}")
        print("=" * 60)
        print("✅ TESTE CONCLUÍDO (sem publicação)")
        print("=" * 60)
        return

    print("\n📤 Upload YouTube...")
    try:
        print("🖼️ Gerando thumbnail...")
        texto_thumb = gerar_texto_thumbnail(titulo_video, tema)
        print(f"   Texto da thumb: {texto_thumb}")
        thumbnail_path = gerar_thumbnail(texto_thumb, termo, f'{ASSETS_DIR}/thumbnail.jpg')

        video_id = fazer_upload_youtube(video_path, titulo, descricao, tags, thumbnail_path)
        url = f'https://youtube.com/{"shorts/" if VIDEO_TYPE == "short" else "watch?v="}{video_id}'
        print(f"✅ Publicado!\n🔗 {url}")

        log_entry = {
            'data': datetime.now().isoformat(), 'tipo': VIDEO_TYPE, 'tema': tema,
            'titulo': titulo, 'duracao': duracao_narracao, 'video_id': video_id, 'url': url
        }
        log_file = 'videos_gerados.json'
        logs = []
        if os.path.exists(log_file):
            with open(log_file, 'r', encoding='utf-8') as f:
                logs = json.load(f)
        logs.append(log_entry)
        with open(log_file, 'w', encoding='utf-8') as f:
            json.dump(logs, f, indent=2, ensure_ascii=False)

    except Exception as e:
        print(f"❌ Erro no upload YouTube: {e}")
        import traceback
        traceback.print_exc()

    print("\n" + "=" * 60)
    print("✅ WORKFLOW CONCLUÍDO")
    print("=" * 60)


if __name__ == '__main__':
    main()
