"""
producao_visual.py
-------------------
Fase 2: fecha a lacuna entre "vídeo com narração e corte de banco de imagem" e
"webdoc" — sem tocar no motor de renderização (isso continua no generate_video.py,
que já tem MoviePy configurado). Este módulo é lógica pura (sem MoviePy, sem I/O de
vídeo), reaproveitando o mesmo relógio mestre que o pipeline já produz: os
timestamps por palavra do faster-whisper.

Três peças, nessa ordem de uso no generate_video.py:

1. mapear_tempos_para_blocos()      — dá a cada bloco do roteiro seu início/fim em
                                       segundos dentro da narração (mesmo pareamento
                                       posicional que gerar_clips_legenda já usa)
2. escolher_termos_por_bloco()      — 1 chamada ao Gemini que escolhe um termo de
                                       busca da lista pré-aprovada PARA CADA bloco
                                       (não mais 1 termo pro vídeo inteiro)
3. escolher_palavras_destaque() +
   resolver_destaques_com_tempo()  — marca palavras/expressões que merecem destaque
                                       visual e resolve o timestamp exato delas
4. construir_timeline_sfx()         — timeline de eventos (troca de bloco, destaque
                                       aparecendo) que o generate_video.py usa pra
                                       disparar SFX no momento certo
"""

import json


def _extrair_json(texto):
    texto = texto.strip().replace('```json', '').replace('```', '').strip()
    inicio = texto.find('{')
    fim = texto.rfind('}') + 1
    if inicio == -1 or fim == 0:
        raise ValueError(f"Nenhum JSON encontrado na resposta: {texto[:200]}")
    return json.loads(texto[inicio:fim])


# ============================================================
# 1. MAPEAR TEMPO DE CADA BLOCO
# ============================================================

def mapear_tempos_para_blocos(blocos, palavras_tempo):
    """
    blocos: lista de {'bloco': str, 'texto': str} (saída do roteiro_engine)
    palavras_tempo: lista de {'inicio': float, 'fim': float}, uma por palavra da
                     narração inteira, na ordem (saída de transcrever_palavras_com_timestamps)

    Retorna os mesmos blocos, acrescidos de:
        inicio, fim, duracao   — em segundos, relativos ao início da narração
        idx_inicio, idx_fim    — índice de palavra (em roteiro.split()) que esse
                                   bloco cobre; usado depois por resolver_destaques_com_tempo
                                   pra procurar a palavra de destaque só dentro do bloco certo
    """
    resultado = []
    cursor = 0
    for bloco in blocos:
        n_palavras = len(bloco['texto'].split())
        fim_cursor = min(cursor + n_palavras, len(palavras_tempo))

        if cursor >= len(palavras_tempo) or fim_cursor <= cursor:
            # roteiro real ficou mais curto que o esperado (raro) — bloco herda o fim do anterior
            inicio = resultado[-1]['fim'] if resultado else 0.0
            fim = inicio
        else:
            inicio = palavras_tempo[cursor]['inicio']
            fim = palavras_tempo[fim_cursor - 1]['fim']

        resultado.append({
            **bloco,
            'inicio': inicio,
            'fim': fim,
            'duracao': max(0.5, fim - inicio),
            'idx_inicio': cursor,
            'idx_fim': fim_cursor,
        })
        cursor = fim_cursor

    return resultado


# ============================================================
# 2. TERMO DE BUSCA POR BLOCO (em vez de 1 termo pro vídeo inteiro)
# ============================================================

def escolher_termos_por_bloco(tema, blocos_com_tempo, termos_validados, gemini_generate_fn):
    if not termos_validados:
        raise Exception("config.json precisa ter 'termos_pesquisa_validados' preenchido")

    blocos_prompt = "\n".join(
        f"[{i}] ({b['bloco']}): {b['texto']}" for i, b in enumerate(blocos_com_tempo)
    )

    prompt = f"""Tema do vídeo: "{tema}"

Escolha, PARA CADA bloco numerado abaixo, o termo MAIS adequado da lista pré-aprovada,
casando o termo com o que aquele trecho específico está dizendo (não com o vídeo inteiro).
Prefira variar entre blocos diferentes — só repita o mesmo termo em dois blocos se
genuinamente não houver opção melhor pra um deles.

LISTA PRÉ-APROVADA:
{json.dumps(termos_validados, ensure_ascii=False)}

BLOCOS:
{blocos_prompt}

Retorne APENAS JSON, um termo por bloco, MESMA ORDEM E QUANTIDADE dos blocos acima,
cada termo EXATAMENTE como aparece na lista:
{{"termos": ["termo do bloco 0", "termo do bloco 1", "..."]}}"""

    try:
        resposta = gemini_generate_fn(prompt)
        termos = _extrair_json(resposta.text).get('termos', [])
    except Exception as e:
        print(f"  ⚠️ Falha ao escolher termos por bloco ({e}) — usando aleatório por bloco")
        termos = []

    import random
    resultado = []
    for i in range(len(blocos_com_tempo)):
        termo = termos[i] if i < len(termos) else None
        if termo not in termos_validados:
            if termo is not None:
                print(f"  ⚠️ Termo fora da lista pro bloco {i} ('{termo}') — usando aleatório")
            termo = random.choice(termos_validados)
        resultado.append(termo)

    return resultado


# ============================================================
# 2.0b TERMO ESPECÍFICO POR BLOCO (pra Wikimedia/Internet Archive) — diferente do termo
# genérico acima (que é sempre um item de uma lista pré-aprovada, pensado pra achar
# B-ROLL GENÉRICO no Pexels: "escritório", "cidade vista de cima"), aqui é uma entidade
# REAL e específica (nome de lugar, evento, órgão, lei) mencionada no próprio bloco —
# sem isso, buscar no Wikimedia com um termo genérico tipo "brazilian city aerial"
# quase nunca acha a imagem RELACIONADA ao fato específico que o bloco está narrando.
# ============================================================

def escolher_termos_especificos_por_bloco(blocos_com_tempo, gemini_generate_fn):
    """
    Extrai, PARA CADA bloco, uma entidade real e específica mencionada no texto (nome
    de cidade/lugar, evento histórico, órgão público, lei, empresa) — usada só como
    query pro Wikimedia Commons/Internet Archive, nunca pro Pexels (que precisa de
    termo genérico de banco de imagem, não de nome próprio). Se o bloco não mencionar
    nada específico o bastante pra valer a pena (ex: um bloco de reflexão genérica),
    o valor fica None — nesse caso _escolher_fonte_midia_alternativa cai pro termo
    genérico do bloco.
    """
    blocos_prompt = "\n".join(
        f"[{i}] ({b['bloco']}): {b['texto']}" for i, b in enumerate(blocos_com_tempo)
    )
    prompt = f"""Para CADA bloco numerado abaixo, extraia UMA entidade real e específica
mencionada no texto — nome de cidade/lugar, evento histórico, órgão público, lei,
empresa, monumento. Isso vai virar uma busca de imagem no Wikimedia Commons, então
precisa ser algo que provavelmente TEM foto lá (lugar/evento/instituição real e
razoavelmente conhecido) — não invente, não force se o bloco não tiver nada assim.

Se o bloco for genérico (reflexão, transição, sem menção específica), retorne null
pra ele.

BLOCOS:
{blocos_prompt}

Retorne APENAS JSON, MESMA ORDEM E QUANTIDADE dos blocos acima:
{{"termos_especificos": ["Nome específico ou null", null, "..."]}}"""

    try:
        resposta = gemini_generate_fn(prompt)
        termos = _extrair_json(resposta.text).get('termos_especificos', [])
    except Exception as e:
        print(f"  ⚠️ Falha ao extrair termos específicos por bloco ({e}) — Wikimedia/Internet "
              f"Archive vão usar o termo genérico do bloco")
        termos = []

    resultado = []
    for i in range(len(blocos_com_tempo)):
        termo = termos[i] if i < len(termos) else None
        resultado.append(termo if isinstance(termo, str) and termo.strip() else None)
    return resultado


# ============================================================
# 2.1 PRINTS DE NOTÍCIA (WEBDOC) — opt-in via config.json 'usar_prints_noticia'
# ============================================================

def decidir_prints_de_noticia(blocos_com_tempo, gemini_generate_fn, usar_prints_noticia=False):
    """
    Fase 3 — SÓ roda se usar_prints_noticia=True (o canal atual de reflexão não passa
    essa flag, então isso fica completamente inerte pra ele).
    Pensado pra webdocs (história, ciência, economia): pergunta ao Gemini quais blocos
    do roteiro descrevem algo que teria virado manchete de jornal — e só esses blocos
    recebem 'usa_print_noticia'=True + uma manchete/subtítulo curtos, gerados a partir
    do próprio texto do bloco (não busca notícia real nenhuma, evita problema de
    direito de imagem — ver mockups_visuais.py).
    Retorna a MESMA lista blocos_com_tempo, só com esses campos adicionados nos blocos
    escolhidos. Se falhar ou a flag estiver desligada, devolve a lista sem alterações.
    """
    if not usar_prints_noticia:
        return blocos_com_tempo

    blocos_prompt = "\n".join(
        f"[{i}] ({b['bloco']}): {b['texto']}" for i, b in enumerate(blocos_com_tempo)
    )

    prompt = f"""Analise os blocos de um roteiro de vídeo abaixo e identifique os blocos que
descrevem um fato, evento ou dado que faria sentido ilustrar com um "print de notícia"
(uma manchete de jornal genérica) — não use isso pra blocos de abertura, reflexão
pessoal, opinião ou fechamento, só pra fatos/eventos concretos.

IMPORTANTE: cada bloco abaixo pode ser um CAPÍTULO INTEIRO (várias centenas de
palavras, cobrindo vários fatos diferentes dentro do mesmo tema) — não é uma frase
curta. Nesse caso, seja generoso: se o capítulo tem 2-3 fatos/dados concretos
citáveis, ESCOLHA-O (você vai apontar qual É a manchete mais forte dali, só uma por
bloco — não precisa esgotar o bloco). Prefira errar pra mais do que pra menos: um
webdoc investigativo tem tipicamente um print de notícia POR CAPÍTULO quando o
capítulo trata de um fato concreto, não é exceção rara.

BLOCOS:
{blocos_prompt}

Para cada bloco escolhido, escreva uma manchete curta (até 10 palavras, estilo jornal,
em CAIXA ALTA) e um subtítulo de uma frase — baseados SÓ no que o bloco já diz, sem
inventar fatos novos.

Retorne APENAS JSON:
{{"escolhidos": [{{"indice": 0, "manchete": "...", "subtitulo": "..."}}]}}
Se nenhum bloco se encaixar (ex: todos são só reflexão/opinião), retorne {{"escolhidos": []}}."""

    try:
        resposta = gemini_generate_fn(prompt)
        escolhidos = _extrair_json(resposta.text).get('escolhidos', [])
    except Exception as e:
        print(f"  ⚠️ Falha ao decidir prints de notícia ({e}) — nenhum bloco vai usar")
        escolhidos = []

    for item in escolhidos:
        i = item.get('indice')
        if isinstance(i, int) and 0 <= i < len(blocos_com_tempo) and item.get('manchete'):
            blocos_com_tempo[i]['usa_print_noticia'] = True
            blocos_com_tempo[i]['manchete_noticia'] = item['manchete'].strip()
            blocos_com_tempo[i]['subtitulo_noticia'] = (item.get('subtitulo') or '').strip()

    if escolhidos:
        print(f"  📰 {len(escolhidos)} bloco(s) vão usar print de notícia")

    return blocos_com_tempo


# ============================================================
# 3. PALAVRAS DE DESTAQUE + RESOLUÇÃO DE TIMESTAMP
# ============================================================

def escolher_palavras_destaque(blocos_com_tempo, gemini_generate_fn, max_por_bloco=2):
    """
    Retorna uma lista de listas (uma por bloco) de expressões curtas (1-3 palavras,
    exatamente como aparecem no texto) que merecem destaque visual — o "grito" na
    tela típico de webdoc, não a legenda inteira.
    """
    blocos_prompt = "\n".join(
        f"[{i}] ({b['bloco']}): {b['texto']}" for i, b in enumerate(blocos_com_tempo)
    )

    prompt = f"""Para cada bloco numerado abaixo, aponte até {max_por_bloco} palavra(s) ou
expressão(ões) curtas (1 a 3 palavras, EXATAMENTE como aparecem no texto, incluindo
pontuação se houver) que merecem destaque visual na tela — números, nomes próprios,
ou a palavra que carrega o argumento central do bloco. NÃO escolha artigos, conectivos
ou palavras genéricas. Se um bloco não tiver nada que mereça destaque, retorne lista vazia.

BLOCOS:
{blocos_prompt}

Retorne APENAS JSON, uma lista por bloco, MESMA ORDEM E QUANTIDADE dos blocos acima:
{{"destaques": [["palavra1"], [], ["palavra2", "palavra3"]]}}"""

    try:
        resposta = gemini_generate_fn(prompt)
        listas = _extrair_json(resposta.text).get('destaques', [])
    except Exception as e:
        print(f"  ⚠️ Falha ao escolher palavras de destaque ({e}) — seguindo sem destaque")
        listas = []

    while len(listas) < len(blocos_com_tempo):
        listas.append([])
    return listas[:len(blocos_com_tempo)]


def _encontrar_subsequencia(lista, alvo):
    """Procura a primeira ocorrência da sequência 'alvo' dentro de 'lista'. None se não achar."""
    n, m = len(lista), len(alvo)
    if m == 0 or m > n:
        return None
    for i in range(n - m + 1):
        if lista[i:i + m] == alvo:
            return i
    return None


def resolver_destaques_com_tempo(roteiro, palavras_tempo, blocos_com_tempo, destaques_por_bloco):
    """
    Converte as expressões de destaque (texto) em timestamps reais, procurando cada
    expressão SÓ dentro da janela de palavras do próprio bloco (idx_inicio:idx_fim),
    pra evitar casar com uma ocorrência da mesma palavra em outro bloco.
    """
    palavras_roteiro = roteiro.split()
    resolvidos = []

    for bloco, frases_destaque in zip(blocos_com_tempo, destaques_por_bloco):
        idx_inicio, idx_fim = bloco['idx_inicio'], bloco['idx_fim']
        janela = [p.strip('.,!?;:"\'').lower() for p in palavras_roteiro[idx_inicio:idx_fim]]

        for frase in frases_destaque:
            alvo = [w.strip('.,!?;:"\'').lower() for w in frase.split()]
            if not alvo:
                continue
            pos = _encontrar_subsequencia(janela, alvo)
            if pos is None:
                continue

            gi = idx_inicio + pos
            gf = gi + len(alvo) - 1
            if gf >= len(palavras_tempo) or gf >= len(palavras_roteiro):
                continue

            resolvidos.append({
                'texto': " ".join(palavras_roteiro[gi:gf + 1]),
                'inicio': palavras_tempo[gi]['inicio'],
                'fim': palavras_tempo[gf]['fim'],
            })

    return resolvidos


# ============================================================
# 4. TIMELINE DE SFX
# ============================================================

def construir_timeline_sfx(blocos_com_tempo, destaques_resolvidos):
    """
    Três tipos de evento, cada um mapeado depois pra uma pasta de SFX própria em
    generate_video.py (assets/sfx/transicao/, assets/sfx/destaque/, assets/sfx/materia/):
      - 'transicao': troca de bloco pra B-roll normal (whoosh)
      - 'destaque':  todo destaque visual que aparece na tela
      - 'materia':   troca de bloco QUE ABRE COM PRINT DE NOTÍCIA (mouse-click, não
                     whoosh — o som de "abrir uma página" combina mais com o mockup de
                     print do que com uma transição de B-roll)
    """
    eventos = []

    for i, bloco in enumerate(blocos_com_tempo):
        if i == 0:  # não dispara SFX de transição no instante zero do vídeo
            continue
        tipo = 'materia' if bloco.get('usa_print_noticia') else 'transicao'
        eventos.append({'tempo': bloco['inicio'], 'tipo': tipo})

    for destaque in destaques_resolvidos:
        eventos.append({'tempo': destaque['inicio'], 'tipo': 'destaque'})

    eventos.sort(key=lambda e: e['tempo'])
    return eventos
