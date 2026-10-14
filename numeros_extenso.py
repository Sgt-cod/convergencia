"""
numeros_extenso.py
-------------------
Rede de segurança DETERMINÍSTICA pra garantir que nenhum dígito chegue ao TTS.

O prompt do roteiro já pede ao Gemini pra escrever todos os números por extenso, mas
LLM não obedece 100% das vezes — e um único "1974" no meio do texto basta pra um TTS
ler errado (ou pro alinhamento com o Whisper desandar). Esta função roda em cima do
texto JÁ GERADO e converte qualquer dígito que tenha escapado.

Hoje só tem regras pra norueguês bokmål (num2words, lang='no'). Pra outro idioma,
devolve o texto intacto (nada quebra) — o canal em português continua exatamente como era.

Uso:
    from numeros_extenso import normalizar_texto_para_tts, contem_digitos
    texto = normalizar_texto_para_tts(texto, idioma='no')
"""

import re

try:
    from num2words import num2words
except ImportError:  # num2words é opcional: sem ele, só as abreviações são tratadas
    num2words = None


_MESES_NO = ('januar|februar|mars|april|mai|juni|juli|august|september|oktober|'
             'november|desember')

# Abreviações comuns em texto norueguês → forma falada. O TTS lê "f.eks." letra por letra.
_ABREVIACOES_NO = [
    (r'\bf\.eks\.', 'for eksempel'),
    (r'\bbl\.a\.', 'blant annet'),
    (r'\bosv\.', 'og så videre'),
    (r'\bdvs\.', 'det vil si'),
    (r'\bca\.', 'cirka'),
    (r'\bmrd\.', 'milliarder'),
    (r'\bmill\.', 'millioner'),
    (r'\bnr\.', 'nummer'),
    (r'\bkl\.', 'klokken'),
    (r'\bpr\.', 'per'),
    (r'\bevt\.', 'eventuelt'),
    (r'\bmfl\.', 'med flere'),
    (r'\bkm/t\b', 'kilometer i timen'),
    (r'\bkm²', 'kvadratkilometer'),
    (r'\bm²', 'kvadratmeter'),
    (r'\bkm\b', 'kilometer'),
    (r'\bkg\b', 'kilo'),
    (r'\bNOK\b', 'kroner'),
    (r'\bkr\.?(?=\s|$|[,.;:!?])', 'kroner'),
]

# Preposições que costumam preceder um ANO (pra distinguir "i 1974" de "1974 personer").
_ANTES_DE_ANO = {'i', 'år', 'fra', 'til', 'siden', 'før', 'etter', 'innen', 'rundt',
                 'omkring', 'mellom', 'og', 'på', 'ved', 'under', 'sommeren', 'høsten',
                 'våren', 'vinteren', 'januar', 'februar', 'mars', 'april', 'mai', 'juni',
                 'juli', 'august', 'september', 'oktober', 'november', 'desember'}


# Se o número de 4 dígitos vem seguido de um desses substantivos, é QUANTIDADE, não ano.
_SUBSTANTIVOS_QUANTIDADE = {'år', 'mennesker', 'personer', 'kroner', 'dollar', 'euro',
                            'meter', 'kilometer', 'døde', 'drepte', 'tonn', 'kilo', 'barn',
                            'menn', 'kvinner', 'innbyggere', 'soldater', 'arbeidere',
                            'hus', 'bygninger', 'skip', 'båter', 'dager', 'timer'}


def _cardinal(n):
    """Cardinal em bokmål, com os ajustes que o num2words deixa passar."""
    t = num2words(int(n), lang='no')
    # num2words devolve "en hundre"/"en tusen"; falado fica "hundre"/"tusen"
    t = re.sub(r'^en (hundre|tusen)\b', r'\1', t)
    t = re.sub(r'\b(og|million|milliard|millioner|milliarder) en (hundre|tusen)\b',
               r'\1 \2', t)
    # plural: "tre million" -> "tre millioner"; "to milliard" -> "to milliarder"
    t = re.sub(r'\b(?!en\b)(\w+) million\b', r'\1 millioner', t)
    t = re.sub(r'\b(?!en\b)(\w+) milliard\b', r'\1 milliarder', t)
    return t


def _ano(n):
    """1100–1999: 'nitten hundre og syttifire'. 2000+: 'to tusen og tjuefire'."""
    if 1100 <= n <= 1999:
        centena, resto = divmod(n, 100)
        base = f"{_cardinal(centena)} hundre"
        return base if resto == 0 else f"{base} og {_cardinal(resto)}"
    return _cardinal(n)


def _ordinal(n):
    return num2words(int(n), lang='no', to='ordinal')


def contem_digitos(texto):
    return bool(re.search(r'\d', texto or ''))


def _normalizar_no(texto):
    if num2words is None:
        return texto

    # 1) abreviações (antes dos números, porque 'kr' e 'mrd.' dependem do número antes)
    for padrao, subst in _ABREVIACOES_NO:
        texto = re.sub(padrao, subst, texto, flags=re.IGNORECASE if padrao.islower() else 0)

    # 2) separador de milhar: "1 200 000", "1.200.000", "1\u00a0200"
    texto = re.sub(r'\b(\d{1,3})(?:[ \u00a0.](\d{3}))+\b',
                   lambda m: re.sub(r'[ \u00a0.]', '', m.group(0)), texto)

    # 3) porcentagem: "12,5 %" / "40%"
    def _pct(m):
        inteiro, dec = m.group(1), m.group(2)
        corpo = _cardinal(inteiro) + (f" komma {_digitos_falados(dec)}" if dec else '')
        return f"{corpo} prosent"
    texto = re.sub(r'(\d+)(?:,(\d+))?\s?%', _pct, texto)

    # 4) data com ordinal: "1. februar" -> "første februar"
    texto = re.sub(rf'\b(\d{{1,2}})\.\s+(?=({_MESES_NO})\b)',
                   lambda m: _ordinal(m.group(1)) + ' ', texto, flags=re.IGNORECASE)

    # 5) ordinal antes de palavra minúscula: "5. mai" já coberto; "1. verdenskrig"
    texto = re.sub(r'\b(\d{1,2})\.\s+(?=[a-zæøå])',
                   lambda m: _ordinal(m.group(1)) + ' ', texto)

    # 6) horário "8:50" / "08:05"
    def _hora(m):
        h, mi = int(m.group(1)), int(m.group(2))
        return _cardinal(h) if mi == 0 else f"{_cardinal(h)} {_cardinal(mi)}"
    texto = re.sub(r'\b(\d{1,2}):(\d{2})\b', _hora, texto)

    # 7) decimal: "3,5" -> "tre komma fem"
    texto = re.sub(r'\b(\d+),(\d+)\b',
                   lambda m: f"{_cardinal(m.group(1))} komma {_digitos_falados(m.group(2))}",
                   texto)

    # 8) anos e cardinais restantes. Anos: 4 dígitos 1100–2099 após preposição de ano,
    #    ou (sem contexto) de 1800 a 2099.
    def _num(m):
        n = int(m.group(0))
        if len(m.group(0)) == 4 and 1100 <= n <= 2099:
            antes = texto[:m.start()].split()
            depois = texto[m.end():].split()
            palavra_antes = antes[-1].lower().strip('.,;:!?()"') if antes else ''
            palavra_depois = depois[0].lower().strip('.,;:!?()"') if depois else ''
            if palavra_depois in _SUBSTANTIVOS_QUANTIDADE:
                return _cardinal(n)
            if palavra_antes in _ANTES_DE_ANO or n >= 1800:
                return _ano(n)
        return _cardinal(n)
    texto = re.sub(r'\d+', _num, texto)

    return texto


def _digitos_falados(digitos):
    """Decimais são lidos dígito a dígito: ',45' -> 'fire fem'."""
    return ' '.join(_cardinal(d) for d in digitos)


def _limpar_pontuacao_solta(texto):
    """Tokens soltos de pontuação (— – ...) contam como 'palavra' em texto.split() mas
    não existem no áudio: desalinham legenda/timestamps. Troca por vírgula."""
    texto = re.sub(r'\s[–—-]\s', ', ', texto)
    texto = texto.replace('…', '...')
    texto = re.sub(r'\s{2,}', ' ', texto)
    return texto.strip()


def normalizar_texto_para_tts(texto, idioma='no'):
    """Devolve o texto sem dígitos/abreviações/pontuação solta. Idioma não suportado
    → texto intacto."""
    if not texto:
        return texto
    if idioma in ('no', 'nb', 'nob', 'norsk'):
        texto = _normalizar_no(texto)
        texto = _limpar_pontuacao_solta(texto)
    return texto
