# -*- coding: utf-8 -*-
"""
fonte_dados.py — camada de VERIFICAÇÃO/ENRIQUECIMENTO, não de descoberta de pauta.

IMPORTANTE (arquitetura do pipeline): este módulo não decide "sobre o que fazer
o vídeo". A pauta nasce em outro lugar — busca na internet, notícia, uma
observação, o que for — e chega aqui já como um recorte específico (ex: "a
cidade X tem uma dependência estranha do Bolsa Família"). O papel desse
módulo é CONFIRMAR e QUANTIFICAR essa pauta com números oficiais e citáveis,
não sugerir do que falar. Fluxo esperado:

    1. Descoberta de pauta (fora daqui): web search / notícia / Gemini
       levanta um candidato — ex: "Município Y pode ser um caso extremo de
       dependência de programa social".
    2. Verificação (aqui): monta_pauta_municipio() busca os números REAIS
       desse município específico nas fontes oficiais — população, %
       Bolsa Família, repasse federal etc. — cada um com a fonte anexada.
    3. Roteiro (roteiro_engine.py): usa os `fatos` confirmados como base
       numérica do roteiro, citando a fonte de cada um. Se um número
       importante pra pauta NÃO tem confirmação aqui (ex: emprego formal —
       ver limitações abaixo), o roteiro deve tratar isso como apuração
       jornalística normal (cita a fonte original da notícia/pesquisa), não
       como dado "confirmado por API".

Todas as fontes usadas aqui são gratuitas:
- IBGE (localidades + agregados/SIDRA): 100% grátis, sem chave.
- Câmara dos Deputados (dados abertos): 100% grátis, sem chave, acesso livre.
- Portal da Transparência: grátis, mas precisa de um token — cadastre um
  e-mail em https://portaldatransparencia.gov.br/api-de-dados/cadastrar-email
  (autenticação via conta gov.br), o token chega por e-mail. Sem isso, as
  funções desse bloco retornam None e o resto do módulo funciona normal.

LIMITAÇÕES CONHECIDAS (não finja que isso aqui cobre tudo que um vídeo
investigativo precisa apurar):
- Emprego formal / CTPS assinada (CAGED, RAIS — Ministério do Trabalho): não
  encontrei uma API REST gratuita e estável pra isso, só microdados pra
  baixar (via basedosdados.org, que tem uma cota gratuita no BigQuery, ou
  direto no site do MTE). Não implementei uma função "chutando" um endpoint
  — se isso for essencial pra pauta, é apuração manual/outro projeto.
- Salário de funcionalismo público MUNICIPAL: o Portal da Transparência só
  cobre servidores do Poder Executivo FEDERAL. Salário de servidor
  municipal está em cada prefeitura — não existe uma API nacional única.
  Querido Diário (https://queridodiario.ok.org.br, API pública, sem chave)
  indexa diários oficiais municipais e pode ajudar a achar publicações de
  folha de pagamento, mas hoje só ~350 municípios têm busca em texto
  completo — o resto só tem os PDFs brutos listados.

Uso típico:

    from fonte_dados import montar_pauta_municipio

    pauta = montar_pauta_municipio("Mariana", "MG", ano=2024)
    print(pauta["fatos"])   # lista de fatos prontos, cada um com sua fonte

Nenhuma função aqui levanta exceção pra fora — cada fonte falha isolada
(rede fora do ar, tabela sem dado pro município, token ausente etc.) e o
restante do cruzamento continua. O chamador sempre recebe um dict, nunca
uma exceção não tratada.
"""

import json
import os
import time

import requests

# ============================================================
# CONFIG — mesmo padrão do generate_video.py (env var > config.json > default)
# ============================================================

CONFIG_FILE = 'config.json'
try:
    with open(CONFIG_FILE, 'r', encoding='utf-8') as _f:
        _config = json.load(_f)
except FileNotFoundError:
    _config = {}

PORTAL_TRANSPARENCIA_TOKEN = os.environ.get(
    'PORTAL_TRANSPARENCIA_TOKEN', _config.get('portal_transparencia_token', '')
)

TIMEOUT_PADRAO = 15  # segundos — essas APIs às vezes demoram, mas não vale travar o pipeline
_CACHE_DIR = '.cache_fonte_dados'  # cache local em disco, evita bater na API toda hora em dev/teste
_CACHE_TTL_SEGUNDOS = 60 * 60 * 24 * 7  # 7 dias — dados de população/legislação não mudam por hora


# ============================================================
# CACHE simples em disco (JSON por chave) — poupa as APIs gratuitas de
# chamadas repetidas durante desenvolvimento/testes do roteiro
# ============================================================

def _cache_ler(chave):
    caminho = os.path.join(_CACHE_DIR, chave + '.json')
    if not os.path.exists(caminho):
        return None
    try:
        idade = time.time() - os.path.getmtime(caminho)
        if idade > _CACHE_TTL_SEGUNDOS:
            return None
        with open(caminho, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _cache_escrever(chave, valor):
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        caminho = os.path.join(_CACHE_DIR, chave + '.json')
        with open(caminho, 'w', encoding='utf-8') as f:
            json.dump(valor, f, ensure_ascii=False)
    except Exception:
        pass  # cache é otimização, não pode derrubar o pipeline se falhar


def _get_json(url, headers=None, params=None, chave_cache=None):
    """GET genérico com cache opcional e falha silenciosa (retorna None + print
    de aviso em vez de exceção) — cada fonte de dado deve continuar funcionando
    mesmo se as outras duas estiverem fora do ar."""
    if chave_cache:
        cacheado = _cache_ler(chave_cache)
        if cacheado is not None:
            return cacheado
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=TIMEOUT_PADRAO)
        resp.raise_for_status()
        dados = resp.json()
        if chave_cache:
            _cache_escrever(chave_cache, dados)
        return dados
    except Exception as e:
        print(f"  ⚠️ fonte_dados: falha ao consultar {url}: {e}")
        return None


# ============================================================
# IBGE — localidades (município → código) e agregados/SIDRA (população etc.)
# Documentação: https://servicodados.ibge.gov.br/api/docs/localidades
#               https://servicodados.ibge.gov.br/api/docs/agregados?versao=3
# ============================================================

def ibge_buscar_municipio(nome_municipio, uf):
    """
    Resolve nome + UF pro código IBGE de 7 dígitos, que é a "chave primária"
    usada pelas outras consultas (SIDRA, e também filtro de município no
    Portal da Transparência). Retorna None se não encontrar.

    Ex: ibge_buscar_municipio("Mariana", "MG") -> {'id': 3140001, 'nome': 'Mariana', 'uf': 'MG'}
    """
    uf = uf.strip().upper()
    chave_cache = f"ibge_municipios_{uf}"
    lista = _get_json(
        f"https://servicodados.ibge.gov.br/api/v1/localidades/estados/{uf}/municipios",
        chave_cache=chave_cache
    )
    if not lista:
        return None

    nome_alvo = nome_municipio.strip().lower()
    for m in lista:
        if m.get('nome', '').strip().lower() == nome_alvo:
            return {'id': m['id'], 'nome': m['nome'], 'uf': uf}

    # fallback: busca parcial, pro caso do nome vir sem acento/com abreviação
    for m in lista:
        if nome_alvo in m.get('nome', '').strip().lower():
            return {'id': m['id'], 'nome': m['nome'], 'uf': uf}

    return None


def ibge_populacao_municipio(codigo_ibge, ano=None):
    """
    População estimada do município (agregado 6579, variável 9324 — série de
    Estimativas de População do IBGE, atualizada anualmente). Se `ano` não for
    passado, pega o período mais recente disponível.

    Retorna {'ano': 2024, 'populacao': 12345} ou None se a tabela não tiver
    dado pra esse município/período (acontece pra municípios muito novos).
    """
    periodo = str(ano) if ano else 'ultimo'
    url = f"https://servicodados.ibge.gov.br/api/v3/agregados/6579/periodos/{periodo}/variaveis/9324"
    dados = _get_json(
        url,
        params={'localidades': f'N6[{codigo_ibge}]'},
        chave_cache=f"ibge_pop_{codigo_ibge}_{periodo}"
    )
    if not dados:
        return None
    try:
        serie = dados[0]['resultados'][0]['series'][0]['serie']
        if not serie:
            return None
        ano_encontrado, valor = list(serie.items())[-1]  # último período retornado
        return {'ano': int(ano_encontrado), 'populacao': int(valor)}
    except (KeyError, IndexError, ValueError, TypeError):
        return None


def ibge_agregado_generico(agregado, variavel, codigo_ibge, periodo='ultimo'):
    """
    Acesso genérico a QUALQUER tabela do SIDRA (agregados IBGE), pra quando
    você precisar de um dado além de população (ex: PIB municipal, IDH,
    lavoura, rebanho...) — a API tem milhares de tabelas.

    Como achar o número do agregado/variável certo pra sua pauta: procure em
    https://sidra.ibge.gov.br pelo indicador que você quer, ou pesquise
    "agregado IBGE <nome do indicador>" — a página da tabela sempre mostra o
    código do agregado e das variáveis disponíveis nela.

    ATENÇÃO: diferente de ibge_populacao_municipio (que já testei e confirmei
    funcionando), essa função genérica não valida se o agregado/variável que
    você passou existe — teste manualmente a URL no navegador antes de usar
    em produção: https://servicodados.ibge.gov.br/api/v3/agregados/{agregado}/periodos/{periodo}/variaveis/{variavel}?localidades=N6[{codigo}]
    """
    url = f"https://servicodados.ibge.gov.br/api/v3/agregados/{agregado}/periodos/{periodo}/variaveis/{variavel}"
    return _get_json(
        url,
        params={'localidades': f'N6[{codigo_ibge}]'},
        chave_cache=f"ibge_agregado_{agregado}_{variavel}_{codigo_ibge}_{periodo}"
    )


# ============================================================
# CÂMARA DOS DEPUTADOS — dados abertos, acesso livre, sem chave
# Documentação: https://dadosabertos.camara.leg.br/swagger/api.html
# ============================================================

_CAMARA_BASE = "https://dadosabertos.camara.leg.br/api/v2"


def camara_buscar_deputado(nome):
    """Busca deputado(s) pelo nome (parcial, sem acento sensível). Retorna
    lista de dicts com id/nome/partido/uf — o 'id' é usado nas outras consultas."""
    dados = _get_json(f"{_CAMARA_BASE}/deputados", params={'nome': nome, 'itens': 20})
    if not dados:
        return []
    return dados.get('dados', [])


def camara_gastos_deputado(id_deputado, ano, mes=None):
    """
    Cota parlamentar (Cota para Exercício da Atividade Parlamentar — CEAP) de
    um deputado específico num ano (e opcionalmente mês). Útil pra vídeos
    tipo "o esquema de gastos de fulano" — cada item vem com fornecedor,
    valor, tipo de despesa e link do documento fiscal (nota fiscal digitalizada).
    """
    params = {'ano': ano, 'itens': 100}
    if mes:
        params['mes'] = mes
    dados = _get_json(
        f"{_CAMARA_BASE}/deputados/{id_deputado}/despesas",
        params=params,
        chave_cache=f"camara_despesas_{id_deputado}_{ano}_{mes or 'todos'}"
    )
    if not dados:
        return []
    return dados.get('dados', [])


def camara_buscar_proposicoes(palavras_chave=None, ano=None, tipo='PL', itens=20):
    """
    Busca projetos de lei (ou outro tipo — PEC, MPV, etc.) por palavra-chave
    e/ou ano. Ótimo pro "por que o Brasil cria tantas leis inúteis" — dá pra
    filtrar por tema e mostrar quantas propostas existem sobre um assunto
    específico, autoria, situação de tramitação etc.

    tipo: 'PL' (Projeto de Lei), 'PEC' (Emenda Constitucional), 'PLP'
    (Lei Complementar), 'MPV' (Medida Provisória) — lista completa de siglas
    em https://dadosabertos.camara.leg.br/api/v2/referencias/proposicoes/siglaTipo
    """
    params = {'siglaTipo': tipo, 'itens': itens, 'ordem': 'DESC', 'ordenarPor': 'id'}
    if ano:
        params['ano'] = ano
    if palavras_chave:
        params['keywords'] = palavras_chave
    dados = _get_json(f"{_CAMARA_BASE}/proposicoes", params=params)
    if not dados:
        return []
    return dados.get('dados', [])


# ============================================================
# PORTAL DA TRANSPARÊNCIA — repasses/convênios do Governo Federal por
# município. PRECISA de token (grátis, ver cabeçalho do arquivo).
# Documentação: https://api.portaldatransparencia.gov.br/swagger-ui/index.html
# ============================================================

_TRANSPARENCIA_BASE = "https://api.portaldatransparencia.gov.br/api-de-dados"


def transparencia_repasses_municipio(codigo_ibge, ano):
    """
    Transferências voluntárias (convênios, repasses) do Governo Federal pro
    município no ano especificado — cada item tem valor, órgão de origem,
    programa e objeto do repasse. É a base de vídeos tipo "a fraude do Minha
    Casa Minha Vida" ou qualquer "pra onde foi o dinheiro público".

    Retorna [] (lista vazia) se PORTAL_TRANSPARENCIA_TOKEN não estiver
    configurado — não quebra o resto do pipeline, só não traz esse dado.
    """
    if not PORTAL_TRANSPARENCIA_TOKEN:
        print("  ℹ️ PORTAL_TRANSPARENCIA_TOKEN não configurado — pulando dados de "
              "repasses (config.json → 'portal_transparencia_token'). Resto da pauta segue normal.")
        return []

    dados = _get_json(
        f"{_TRANSPARENCIA_BASE}/transferencias-voluntarias",
        headers={'chave-api-dados': PORTAL_TRANSPARENCIA_TOKEN},
        params={'municipio': codigo_ibge, 'ano': ano, 'pagina': 1, 'itens': 50},
        chave_cache=f"transparencia_repasses_{codigo_ibge}_{ano}"
    )
    return dados or []


def transparencia_bolsa_familia_municipio(codigo_ibge, ano, mes):
    """
    Beneficiários do Bolsa Família por município/mês — endpoint
    'novo-bolsa-familia-sacado-beneficiario-por-municipio', confirmado no
    changelog oficial da API (https://api.portaldatransparencia.gov.br/changelog).

    Retorna a lista de parcelas sacadas naquele mês/ano pro município — cada
    item traz o valor da parcela; o TAMANHO da lista (len(resultado)) é
    aproximadamente o número de beneficiários naquele mês (uma parcela por
    beneficiário/família). Pra virar "93% da população recebe Bolsa
    Família" como no exemplo do Elementar, cruze len(resultado) com a
    população do ibge_populacao_municipio() — mas normalize a base de
    comparação (Bolsa Família é por FAMÍLIA, população é por PESSOA; não dá
    pra dividir direto um pelo outro sem essa ressalva no roteiro).

    Retorna [] se o token não estiver configurado, igual às outras funções
    desse bloco.
    """
    if not PORTAL_TRANSPARENCIA_TOKEN:
        print("  ℹ️ PORTAL_TRANSPARENCIA_TOKEN não configurado — pulando dados de "
              "Bolsa Família (config.json → 'portal_transparencia_token').")
        return []

    mes_ano = f"{int(ano)}{int(mes):02d}"
    dados = _get_json(
        f"{_TRANSPARENCIA_BASE}/novo-bolsa-familia-sacado-beneficiario-por-municipio",
        headers={'chave-api-dados': PORTAL_TRANSPARENCIA_TOKEN},
        params={'codigoIbge': codigo_ibge, 'mesAno': mes_ano, 'pagina': 1},
        chave_cache=f"transparencia_bolsafamilia_{codigo_ibge}_{mes_ano}"
    )
    return dados or []


# ============================================================
# CRUZAMENTO — a parte que realmente entrega valor: uma pauta pronta,
# cruzando as 3 fontes pelo código IBGE do município, com FONTE explícita em
# cada fato (importante pro roteiro citar de onde veio, e pra você conferir
# antes de publicar).
# ============================================================

def montar_pauta_municipio(nome_municipio, uf, ano=None, mes_bolsa_familia=None):
    """
    Função principal do módulo — RECEBE um município já definido por outra
    etapa (pauta apurada na internet/redação) e devolve os números OFICIAIS
    que confirmam/quantificam essa pauta, cruzando:
    - IBGE: código do município + população estimada
    - Portal da Transparência: repasses federais recebidos no ano, e
      beneficiários do Bolsa Família num mês específico (se token configurado)

    Retorna um dict:
    {
        'municipio': 'Mariana', 'uf': 'MG', 'codigo_ibge': 3140001,
        'fatos': [
            {'texto': 'Mariana (MG) tem população estimada de 62.116 habitantes (2024).',
             'fonte': 'IBGE - Estimativas de População'},
            {'texto': 'Mariana recebeu R$ 4.230.000,00 em repasses federais em 2024.',
             'fonte': 'Portal da Transparência - Transferências Voluntárias'},
        ],
        'dados_brutos': {...}  # os dicts originais de cada API, pra quem quiser ir além dos 'fatos'
    }

    `mes_bolsa_familia`: mês (1-12) de referência pra puxar beneficiários do
    Bolsa Família. Se None, essa fonte não é consultada (custa uma chamada de
    API extra, então só busca se você realmente vai usar esse número).

    Se uma fonte falhar/não tiver dado, ela simplesmente não aparece em
    'fatos' — a função nunca lança exceção. E lembre: os `fatos` daqui são
    NÚMEROS CONFIRMADOS, não a pauta em si — a decisão de "esse município é
    uma boa pauta" já devia ter sido tomada antes de chamar essa função.
    """
    ano = ano or time.localtime().tm_year - 1  # ano anterior por padrão (dado do ano corrente costuma estar incompleto)
    resultado = {
        'municipio': nome_municipio, 'uf': uf.upper(), 'codigo_ibge': None,
        'fatos': [], 'dados_brutos': {}
    }

    municipio = ibge_buscar_municipio(nome_municipio, uf)
    if not municipio:
        print(f"  ⚠️ fonte_dados: não achei '{nome_municipio}/{uf}' no IBGE — "
              f"confira a grafia (tem que bater com o nome oficial do município)")
        return resultado

    resultado['codigo_ibge'] = municipio['id']
    resultado['dados_brutos']['ibge_municipio'] = municipio

    pop = ibge_populacao_municipio(municipio['id'])
    if pop:
        resultado['dados_brutos']['ibge_populacao'] = pop
        resultado['fatos'].append({
            'texto': f"{municipio['nome']} ({uf.upper()}) tem população estimada de "
                     f"{pop['populacao']:,}".replace(',', '.') + f" habitantes ({pop['ano']}).",
            'fonte': 'IBGE - Estimativas de População'
        })

    repasses = transparencia_repasses_municipio(municipio['id'], ano)
    if repasses:
        total = sum(float(r.get('valorRecebido', 0) or 0) for r in repasses)
        resultado['dados_brutos']['transparencia_repasses'] = repasses
        resultado['fatos'].append({
            'texto': f"{municipio['nome']} recebeu R$ {total:,.2f}".replace(',', '#').replace('.', ',').replace('#', '.')
                     + f" em transferências voluntárias do Governo Federal em {ano}, "
                       f"em {len(repasses)} repasse(s).",
            'fonte': 'Portal da Transparência - Transferências Voluntárias'
        })

    if mes_bolsa_familia:
        beneficiarios = transparencia_bolsa_familia_municipio(municipio['id'], ano, mes_bolsa_familia)
        if beneficiarios:
            resultado['dados_brutos']['transparencia_bolsa_familia'] = beneficiarios
            resultado['fatos'].append({
                'texto': f"{municipio['nome']} teve {len(beneficiarios)} família(s) "
                         f"beneficiária(s) do Bolsa Família em {mes_bolsa_familia:02d}/{ano} "
                         f"(nº de famílias, não de pessoas — não divida direto pela população).",
                'fonte': 'Portal da Transparência - Novo Bolsa Família'
            })

    if not resultado['fatos']:
        print(f"  ℹ️ fonte_dados: nenhum fato cruzado pra {nome_municipio}/{uf} — "
              f"município existe no IBGE mas as outras fontes não trouxeram dado "
              f"(normal se não houver repasse federal registrado nesse ano, ou "
              f"se o token do Portal da Transparência não estiver configurado)")

    return resultado


if __name__ == '__main__':
    # Teste manual rápido: python fonte_dados.py "Mariana" MG
    import sys
    if len(sys.argv) >= 3:
        pauta = montar_pauta_municipio(sys.argv[1], sys.argv[2])
        print(json.dumps(pauta, ensure_ascii=False, indent=2))
    else:
        print("Uso: python fonte_dados.py <municipio> <UF>")
        print("Ex:  python fonte_dados.py Mariana MG")
