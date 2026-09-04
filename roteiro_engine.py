"""
roteiro_engine.py
------------------
Substitui as funções gerar_titulo() / gerar_roteiro() de uma chamada única por uma
cadeia de 5 estágios (tese -> estrutura -> escrita -> crítica adversarial -> título/descrição).

Motivo: uma única chamada "escreva um roteiro sobre X" tende a produzir resumo com
tom dramático por cima — a textura genérica que qualquer LLM produz quando não é
forçado a assumir uma posição específica. Separar em passes obrigatórios resolve isso
porque cada passe sozinho é uma tarefa mais fácil de fazer bem do que pedir tudo junto.

Não depende de nada específico do generate_video.py — recebe a função de chamada ao
Gemini (com retry já embutido) como parâmetro, pra reusar exatamente o _gemini_generate()
que já existe lá, sem duplicar lógica de retry/backoff.

Uso no generate_video.py:

    from roteiro_engine import gerar_pacote_roteiro

    pacote = gerar_pacote_roteiro(
        tema=tema,
        contexto_nicho=CONTEXTO_NICHO,
        idioma_conteudo=IDIOMA_CONTEUDO,
        instrucao_extra=INSTRUCAO_EXTRA_ROTEIRO,
        documento_estilo=config.get('documento_estilo', []),
        tipo_video=VIDEO_TYPE,
        gemini_generate_fn=_gemini_generate,
    )

    roteiro = pacote['roteiro_texto']       # string corrida, pronta pra TTS (compatível com o resto do código)
    titulo_video = pacote['titulo']         # string, já escolhido entre as variantes
    descricao_extra = pacote['descricao']   # dict {abertura_seo, corpo}
    blocos_roteiro = pacote['roteiro_blocos']  # lista de {bloco, texto} — pra Fase 2 (casar B-roll por bloco)

Se qualquer estágio falhar, cai para geração simples (comportamento antigo) em vez de
quebrar o pipeline — mesma filosofia de fallback que já existe no resto do código.
"""

import json
import re


def _extrair_json(texto):
    """Mesmo padrão já usado em gerar_titulo() no generate_video.py: acha o primeiro { ... }."""
    texto = texto.strip().replace('```json', '').replace('```', '').strip()
    inicio = texto.find('{')
    fim = texto.rfind('}') + 1
    if inicio == -1 or fim == 0:
        raise ValueError(f"Nenhum JSON encontrado na resposta: {texto[:200]}")
    return json.loads(texto[inicio:fim])


# ============================================================
# ESTÁGIO 1 — TESE
# ============================================================

def gerar_tese(tema, contexto_nicho, idioma_conteudo, gemini_generate_fn):
    prompt = f"""Você é um pesquisador cético, não um narrador. Sua única tarefa é produzir
uma AFIRMAÇÃO específica e refutável sobre o tema abaixo — nunca um resumo ou generalidade.

NICHO: {contexto_nicho}
TEMA: "{tema}"

Regras rígidas:
- A afirmação precisa poder estar ERRADA. Se ninguém discordaria dela, rejeite-a.
- Proibido gerar afirmação do tipo "{tema} é importante" ou "{tema} muda tudo".
- A afirmação deve conectar uma causa específica a um efeito específico, ou revelar uma
  crença comum sobre o tema que é enganosa/incompleta.
- Escreva em {idioma_conteudo}.

Retorne APENAS JSON, neste formato exato:
{{
  "tese": "uma frase, a afirmação específica",
  "por_que_e_contestavel": "o que alguém poderia usar para discordar",
  "reprovar": false
}}

Se genuinamente não for possível gerar uma tese específica para esse tema (é raro),
retorne "reprovar": true e explique o motivo em "tese"."""

    resposta = gemini_generate_fn(prompt)
    return _extrair_json(resposta.text)


# ============================================================
# ESTÁGIO 2 — ESTRUTURA ARGUMENTATIVA
# ============================================================

def gerar_estrutura(tese_dict, contexto_nicho, idioma_conteudo, gemini_generate_fn):
    prompt = f"""Você recebeu uma tese. Construa o esqueleto argumentativo do vídeo.
NÃO escreva prosa ainda — apenas a estrutura lógica, em {idioma_conteudo}.

NICHO: {contexto_nicho}
TESE: {tese_dict['tese']}
POR QUE É CONTESTÁVEL: {tese_dict['por_que_e_contestavel']}

Retorne APENAS JSON com estas 6 chaves (cada uma uma string curta, 1-2 frases):
{{
  "gancho": "a tensão ou pergunta que abre o vídeo — NÃO pode ser a tese repetida",
  "evidencia_a_favor": "o ponto principal que sustenta a tese",
  "objecao": "o argumento mais forte CONTRA a tese, ou a crença comum que ela contraria",
  "resposta_a_objecao": "como a tese sobrevive à objeção",
  "implicacao": "por que isso importa pra quem está assistindo, hoje, na prática",
  "fechamento": "a ideia final — não uma frase motivacional vaga, algo específico e acionável"
}}"""

    resposta = gemini_generate_fn(prompt)
    return _extrair_json(resposta.text)


# ============================================================
# ESTÁGIO 3 — ESCRITA (só aqui vira prosa)
# ============================================================

def gerar_prosa(estrutura, contexto_nicho, idioma_conteudo, instrucao_extra,
                 documento_estilo, palavras_alvo, gemini_generate_fn):
    """
    Genérica em relação à estrutura: funciona tanto com a estrutura argumentativa de 6
    chaves (Estágio 2 / modo cadeia_completa) quanto com qualquer outra estrutura em
    blocos, como a devocional de 4 chaves (gerar_estrutura_devocional / modo simples).
    O único contrato é: estrutura é um dict ordenado {chave: descrição}, e cada chave
    vira um bloco do roteiro final, na mesma ordem.
    """
    bloco_estilo = ""
    if documento_estilo:
        exemplos = "\n\n".join(f"- {ex}" for ex in documento_estilo)
        bloco_estilo = f"""
DOCUMENTO DE ESTILO — use isso como referência de TOM E VOZ (vocabulário, cadência,
tipo de imagem usada), NÃO como referência de TAMANHO. Os exemplos abaixo podem ser
mais curtos que a meta de palavras pedida mais adiante — nesse caso, desenvolva mais
os mesmos pontos na mesma voz, em vez de encurtar pra bater com o tamanho do exemplo:
{exemplos}
"""

    linha_extra = f"\n- {instrucao_extra}" if instrucao_extra else ""
    n_blocos = max(1, len(estrutura))
    palavras_por_bloco = max(15, round(palavras_alvo / n_blocos))
    itens_estrutura = "\n".join(f"{i+1}. {chave}: {valor}" for i, (chave, valor) in enumerate(estrutura.items()))
    blocos_exemplo = ",\n    ".join(f'{{"bloco": "{chave}", "texto": "..."}}' for chave in estrutura.keys())

    prompt = f"""Escreva o roteiro de narração seguindo ESTRITAMENTE a estrutura abaixo, em {idioma_conteudo}.

NICHO: {contexto_nicho}
{bloco_estilo}
ESTRUTURA (siga esta ordem — cada item vira um bloco de narração de ~{palavras_por_bloco}
palavras, NÃO um parágrafo curto de 1-2 frases):
{itens_estrutura}

REGRAS OBRIGATÓRIAS:
- Duração alvo: ~{palavras_alvo} palavras no total, distribuídas quase igualmente entre
  os {n_blocos} blocos acima (~{palavras_por_bloco} palavras cada) — isso é mais importante
  que soar "conciso"; desenvolva cada ideia com exemplos e detalhes concretos até chegar lá
- PROIBIDO usar: "mas o que isso realmente significa?", "e é aí que tudo muda",
  "prepare-se para descobrir", ou qualquer frase que serviria em vídeo sobre qualquer
  outro tema do mesmo nicho
- Frases curtas, sem formatação, sem asteriscos, sem emojis
- NÃO mencione apresentador, câmera ou elementos visuais{linha_extra}

Retorne APENAS JSON:
{{
  "blocos": [
    {blocos_exemplo}
  ]
}}"""

    resposta = gemini_generate_fn(prompt)
    dados = _extrair_json(resposta.text)
    blocos = dados['blocos']

    padrao_prefixo = re.compile(r'^\s*\[\d+\]\s*\([^)]*\)\s*:\s*')
    for b in blocos:
        b['texto'] = re.sub(r'\*+', '', b['texto'])
        b['texto'] = b['texto'].replace('#', '').replace('_', '').strip()
        b['texto'] = padrao_prefixo.sub('', b['texto']).strip()

    total_palavras = sum(len(b['texto'].split()) for b in blocos)
    if total_palavras < palavras_alvo * 0.7:
        print(f"  ⚠️ Roteiro saiu com {total_palavras} palavras (meta: ~{palavras_alvo}) — "
              f"o vídeo final vai ficar mais curto que o esperado. Se isso persistir, "
              f"considere reduzir os exemplos de 'documento_estilo' ou torná-los mais longos.")

    return blocos


# ============================================================
# ESTÁGIOS 1-2 (MODO SIMPLES) — sem forçar tese contestável/objeção
# ============================================================

def gerar_estrutura_devocional(tema, contexto_nicho, idioma_conteudo, gemini_generate_fn):
    """
    Alternativa aos Estágios 1+2 (tese + estrutura argumentativa) pra nichos onde
    forçar uma afirmação contestável soa artificial — ex: reflexão devocional, onde
    o valor não vem de defender uma tese contra uma objeção, vem de uma virada de
    perspectiva sobre algo familiar. Ainda produz uma estrutura EM BLOCOS (isso é o
    que a Fase 2 de produção visual precisa pra casar B-roll/destaque por trecho) —
    só que sem contradição forçada.
    """
    prompt = f"""Construa o esqueleto de uma reflexão curta sobre o tema abaixo, em {idioma_conteudo}.
NÃO escreva prosa ainda — apenas a estrutura.

NICHO: {contexto_nicho}
TEMA: "{tema}"

Regras:
- Evite abrir com pergunta genérica tipo "você já parou pra pensar..."
- A reflexão central deve ter um ângulo específico sobre o tema, não uma generalidade
  que serviria pra qualquer tema parecido
- A aplicação prática deve ser concreta (algo pra fazer/observar hoje), não vaga

Retorne APENAS JSON com estas 4 chaves:
{{
  "abertura": "uma imagem, cena cotidiana ou observação concreta que introduz o tema",
  "reflexao_central": "a ideia principal sobre o tema, com um ângulo específico",
  "aplicacao_pratica": "algo concreto que a pessoa pode fazer ou observar hoje",
  "fechamento": "uma frase final que não seja um clichê motivacional vago"
}}"""

    resposta = gemini_generate_fn(prompt)
    return _extrair_json(resposta.text)


# ============================================================
# ESTÁGIOS 1-2 (MODO WEBDOC EM CAPÍTULOS) — formato investigativo tipo
# "Elementar": introdução com dado forte, N capítulos nomeados (cada um cobrindo
# um ÂNGULO diferente do tema, não um passo de argumentação), desfecho.
# ============================================================

def gerar_estrutura_capitulos(tema, contexto_nicho, idioma_conteudo, gemini_generate_fn, num_capitulos=3):
    """
    Estrutura alternativa à argumentativa (tese/objeção) e à devocional (abertura/
    reflexão) — aqui a lógica é jornalística/investigativa: cada capítulo cobre uma
    FACETA distinta do tema (origem do problema, como funciona hoje, quem é afetado,
    comparação com outro caso etc.), não um degrau de um argumento.

    O título de cada capítulo (curto, tipo rótulo de seção de documentário — "A
    Fábrica de Prédios", "O Esquema do Repasse") é o que vira o CARD PRETO de
    transição entre capítulos no vídeo final, e também dá nome à pasta/critério de
    música tema daquele trecho (ver generate_video.py → gerar_card_capitulo /
    _mixar_musica_por_capitulo).
    """
    prompt = f"""Você é roteirista de documentário investigativo de dados (estilo canais
como "Elementar" no YouTube — vídeos tipo "Por que X está assim?", "A fraude de Y",
"O esquema de Z", sempre com dado oficial concreto por trás). NÃO escreva prosa ainda —
apenas o esqueleto, em {idioma_conteudo}.

NICHO: {contexto_nicho}
TEMA: "{tema}"

Construa {num_capitulos} capítulos, cada um cobrindo uma FACETA DIFERENTE do tema —
escolha os {num_capitulos} ângulos mais fortes especificamente pra ESTE tema (não uma
lista genérica que serviria pra qualquer tema do nicho).

Regras:
- O título de cada capítulo tem 3 a 6 palavras, como um RÓTULO de seção de documentário
  — não é uma frase completa (ex: "A Fábrica de Prédios", não "Por que os prédios são
  construídos assim")
- A introdução deve abrir com um DADO ou FATO concreto e específico sobre o tema, não
  uma pergunta genérica
- O desfecho deve responder à pergunta implícita do vídeo, não só resumir os capítulos

Retorne APENAS JSON:
{{
  "introducao": "o que a introdução deve cobrir — o dado forte de abertura + o tema",
  "capitulos": [
    {{"titulo": "Título Curto do Capítulo 1", "cobre": "que faceta/ângulo este capítulo aborda, em detalhe suficiente pra escrever a prosa depois"}}
  ],
  "desfecho": "o que o desfecho deve responder/concluir"
}}
(o array "capitulos" deve ter exatamente {num_capitulos} itens)"""

    resposta = gemini_generate_fn(prompt)
    return _extrair_json(resposta.text)


def gerar_prosa_capitulos(estrutura_capitulos, contexto_nicho, idioma_conteudo, instrucao_extra,
                           documento_estilo, palavras_alvo, gemini_generate_fn):
    """
    Escreve a prosa de introdução/capítulos/desfecho reaproveitando gerar_prosa (mesmo
    contrato: dict ordenado {{chave: descrição}} -> um bloco de texto por chave), e
    depois anota cada bloco de capítulo com 'titulo_capitulo' + 'inicio_capitulo': True.

    Esses dois metadados extras (que sobrevivem até blocos_com_tempo, porque
    mapear_tempos_para_blocos faz **bloco ao montar o resultado) são o que
    generate_video.py usa pra saber ONDE no vídeo inserir o card preto de transição e
    trocar a música tema — sem precisar mexer em nada do pipeline de TTS/áudio: o
    timestamp certo já vem de graça do mapeamento por contagem de palavras que já existe.

    Truque de sincronismo: o título do capítulo entra FALADO no início do próprio bloco
    ("A Fábrica de Prédios. " + resto do texto) — assim o card visual, que aparece
    exatamente no timestamp de início do bloco, casa com o que o narrador está dizendo
    naquele instante, em vez de aparecer "solto" sobre uma frase qualquer do meio do
    capítulo.
    """
    estrutura_prosa = {'introducao': estrutura_capitulos['introducao']}
    for i, cap in enumerate(estrutura_capitulos['capitulos']):
        estrutura_prosa[f'capitulo_{i + 1}'] = cap['cobre']
    estrutura_prosa['desfecho'] = estrutura_capitulos['desfecho']

    blocos = gerar_prosa(estrutura_prosa, contexto_nicho, idioma_conteudo, instrucao_extra,
                          documento_estilo, palavras_alvo, gemini_generate_fn)

    titulos_capitulos = {f'capitulo_{i + 1}': cap['titulo']
                          for i, cap in enumerate(estrutura_capitulos['capitulos'])}
    for b in blocos:
        titulo_cap = titulos_capitulos.get(b['bloco'])
        b['titulo_capitulo'] = titulo_cap
        b['inicio_capitulo'] = titulo_cap is not None
        if titulo_cap:
            b['texto'] = f"{titulo_cap}. {b['texto']}"

    return blocos


def gerar_pacote_roteiro_capitulos(tema, contexto_nicho, idioma_conteudo, instrucao_extra,
                                    documento_estilo, tipo_video, gemini_generate_fn,
                                    num_capitulos=3):
    """
    Modo 'capitulos_webdoc' — formato investigativo em capítulos nomeados, cada um com
    card de transição e música própria. Mesmo contrato de saída de gerar_pacote_roteiro
    (roteiro_texto, roteiro_blocos, titulo, descricao), mais 'capitulos_meta' — lista
    pronta de {{'titulo', 'bloco'}} pra quem não quiser vasculhar roteiro_blocos.
    """
    palavras_alvo = 180 if tipo_video == 'short' else 900  # webdoc investigativo tende a ser mais longo (12-20min)

    try:
        print("  🧱 Estágio 1/4 — estrutura em capítulos...")
        estrutura = gerar_estrutura_capitulos(tema, contexto_nicho, idioma_conteudo,
                                               gemini_generate_fn, num_capitulos)

        print("  ✍️ Estágio 2/4 — escrita...")
        blocos = gerar_prosa_capitulos(estrutura, contexto_nicho, idioma_conteudo, instrucao_extra,
                                        documento_estilo, palavras_alvo, gemini_generate_fn)

        print("  🔍 Estágio 3/4 — crítica adversarial...")
        blocos = criticar_e_reescrever(blocos, idioma_conteudo, gemini_generate_fn)
        # criticar_e_reescrever reescreve 'texto' inteiro — se a reescrita tiver comido o
        # título falado no começo do bloco, recoloca (comparação simples de prefixo)
        for b in blocos:
            titulo_cap = b.get('titulo_capitulo')
            if titulo_cap and not b['texto'].lstrip().startswith(titulo_cap):
                b['texto'] = f"{titulo_cap}. {b['texto']}"

        print("  🏷️ Estágio 4/4 — título e descrição...")
        titulo = gerar_titulo_final_simples(tema, estrutura, idioma_conteudo, gemini_generate_fn)
        descricao = gerar_descricao_final_simples(tema, estrutura, idioma_conteudo, gemini_generate_fn)

        roteiro_texto = " ".join(b['texto'] for b in blocos)
        capitulos_meta = [{'titulo': b['titulo_capitulo'], 'bloco': b['bloco']}
                           for b in blocos if b.get('inicio_capitulo')]

        return {
            'roteiro_texto': roteiro_texto,
            'roteiro_blocos': blocos,
            'titulo': titulo,
            'descricao': descricao,
            'tese': None,
            'modo': 'capitulos_webdoc',
            'capitulos_meta': capitulos_meta,
        }
    except Exception as e:
        print(f"  ⚠️ Cadeia de capítulos falhou ({e}) — usando geração simples de emergência")
        prompt_simples = f"""Crie um roteiro de narração investigativo para um vídeo de {contexto_nicho}
sobre "{tema}", em {idioma_conteudo}, ~{palavras_alvo} palavras, tom direto e factual.
Escreva APENAS o roteiro corrido."""
        resposta = gemini_generate_fn(prompt_simples)
        roteiro_texto = re.sub(r'\*+', '', resposta.text).replace('#', '').strip()
        return {
            'roteiro_texto': roteiro_texto,
            'roteiro_blocos': [{'bloco': 'roteiro_completo', 'texto': roteiro_texto}],
            'titulo': tema,
            'descricao': {'abertura_seo': tema, 'corpo': roteiro_texto[:200]},
            'tese': None,
            'modo': 'fallback_simples',
            'capitulos_meta': [],
        }


def gerar_titulo_final_simples(tema, estrutura, idioma_conteudo, gemini_generate_fn):
    prompt = f"""Gere 10 variantes de título de vídeo para YouTube, em {idioma_conteudo}.
Cada variante prioriza EXPLICITAMENTE um destes critérios (rotule qual):
- gap_curiosidade: cria uma lacuna de informação sem entregar a resposta
- especificidade: usa um detalhe concreto do vídeo
- emocao: nomeia o sentimento/estado que o vídeo aborda

TEMA: {tema}
REFLEXÃO CENTRAL: {estrutura.get('reflexao_central', '')}
APLICAÇÃO PRÁTICA: {estrutura.get('aplicacao_pratica', '')}

Para cada variante, responda também: esse título serviria pra QUALQUER vídeo desse
nicho, ou só faz sentido pra ESTE vídeo específico?

Retorne APENAS JSON:
{{
  "titulos": [
    {{"texto": "...", "criterio": "...", "exclusivo_deste_video": true}}
  ]
}}"""

    resposta = gemini_generate_fn(prompt)
    dados = _extrair_json(resposta.text)
    candidatos = [t for t in dados.get('titulos', []) if t.get('exclusivo_deste_video')]
    if not candidatos:
        candidatos = dados.get('titulos', [])
    if not candidatos:
        return tema[:60]
    return candidatos[0]['texto']


def gerar_descricao_final_simples(tema, estrutura, idioma_conteudo, gemini_generate_fn):
    prompt = f"""Escreva a descrição do vídeo em duas partes, em {idioma_conteudo}.

TEMA: {tema}
REFLEXÃO CENTRAL: {estrutura.get('reflexao_central', '')}
APLICAÇÃO PRÁTICA: {estrutura.get('aplicacao_pratica', '')}

1. abertura_seo (até 150 caracteres): termos de busca reais que alguém digitaria sobre
   esse tema — não invente termos.
2. corpo: o que o espectador vai encontrar no vídeo, sem entregar a reflexão inteira.

Retorne APENAS JSON: {{"abertura_seo": "...", "corpo": "..."}}"""

    resposta = gemini_generate_fn(prompt)
    return _extrair_json(resposta.text)


# ============================================================
# ESTÁGIO 4 — CRÍTICA ADVERSARIAL + REESCRITA CIRÚRGICA
# ============================================================

def criticar_e_reescrever(blocos, idioma_conteudo, gemini_generate_fn):
    """
    Chamada separada da escrita de propósito: crítica e geração são tarefas cognitivas
    diferentes, e o mesmo prompt que gera o clichê raramente enxerga o próprio clichê.
    Só reescreve os blocos marcados com problema — nunca o roteiro inteiro de novo.
    """
    roteiro_numerado = "\n".join(f"[{i}] ({b['bloco']}): {b['texto']}" for i, b in enumerate(blocos))

    prompt = f"""Você é um editor implacável. Sua ÚNICA função é achar defeito, nunca elogiar
nem reescrever ainda.

ROTEIRO (em {idioma_conteudo}, um bloco numerado por linha):
{roteiro_numerado}

Para cada bloco com problema, marque o tipo:
- clichê: frase que poderia estar em qualquer vídeo do nicho
- ritmo_quebrado: frase longa demais ou estrutura repetitiva
- redundancia: informação repetida sem necessidade

Retorne APENAS JSON, lista de problemas (vazio se não houver nenhum):
{{"problemas": [{{"indice": 0, "tipo": "cliche", "motivo": "..."}}]}}"""

    resposta = gemini_generate_fn(prompt)
    problemas = _extrair_json(resposta.text).get('problemas', [])

    if not problemas:
        return blocos

    indices_com_problema = sorted({p['indice'] for p in problemas if 0 <= p['indice'] < len(blocos)})
    if not indices_com_problema:
        return blocos

    trechos = "\n".join(
        f"[{i}] ({blocos[i]['bloco']}): {blocos[i]['texto']}\n"
        f"    problema: {[p['motivo'] for p in problemas if p['indice'] == i]}"
        for i in indices_com_problema
    )

    prompt_reescrita = f"""Reescreva APENAS os blocos abaixo, corrigindo o problema apontado.
Mantenha o mesmo sentido e o mesmo tamanho aproximado. Escreva em {idioma_conteudo}.

{trechos}

IMPORTANTE — formato da resposta: "texto_novo" deve ser SÓ o texto puro que vai ser
narrado, exatamente como sairia numa legenda. NUNCA repita o prefixo "[n] (nome_do_bloco):"
usado acima pra identificar os trechos — isso é só uma referência, não faz parte do roteiro.
Não inclua colchetes, números de índice, nem o nome do bloco entre parênteses.

Retorne APENAS JSON: {{"reescritas": [{{"indice": 0, "texto_novo": "..."}}]}}"""

    resposta2 = gemini_generate_fn(prompt_reescrita)
    reescritas = _extrair_json(resposta2.text).get('reescritas', [])

    # Defesa extra: mesmo com a instrução acima, o modelo às vezes ainda ecoa o
    # prefixo "[n] (bloco): " de volta no texto_novo — se isso acontecer, o prefixo
    # vazaria pra narração E pra legenda (roteiro_texto alimenta as duas). Removemos
    # qualquer prefixo nesse formato antes de aceitar o texto.
    padrao_prefixo = re.compile(r'^\s*\[\d+\]\s*\([^)]*\)\s*:\s*')

    for r in reescritas:
        i = r.get('indice')
        if isinstance(i, int) and 0 <= i < len(blocos) and r.get('texto_novo'):
            texto_novo = padrao_prefixo.sub('', r['texto_novo'].strip())
            blocos[i]['texto'] = texto_novo.strip()

    return blocos


# ============================================================
# ESTÁGIO 5 — TÍTULO E DESCRIÇÃO (pipeline próprio)
# ============================================================

def gerar_titulo_final(tese_dict, estrutura, idioma_conteudo, gemini_generate_fn):
    prompt = f"""Gere 10 variantes de título de vídeo para YouTube, em {idioma_conteudo}.
Cada variante prioriza EXPLICITAMENTE um destes critérios (rotule qual):
- gap_curiosidade: cria uma lacuna de informação sem entregar a resposta
- especificidade: usa um detalhe concreto do vídeo
- contraste: usa a objeção/crença comum que a tese contraria

TESE: {tese_dict['tese']}
OBJEÇÃO: {estrutura['objecao']}
GANCHO: {estrutura['gancho']}

Para cada variante, responda também: esse título serviria pra QUALQUER vídeo desse nicho,
ou só faz sentido pra ESTE vídeo específico?

Retorne APENAS JSON:
{{
  "titulos": [
    {{"texto": "...", "criterio": "...", "exclusivo_deste_video": true}}
  ]
}}"""

    resposta = gemini_generate_fn(prompt)
    dados = _extrair_json(resposta.text)
    candidatos = [t for t in dados.get('titulos', []) if t.get('exclusivo_deste_video')]

    if not candidatos:
        candidatos = dados.get('titulos', [])
    if not candidatos:
        return tese_dict['tese'][:60]

    return candidatos[0]['texto']


def gerar_descricao_final(tese_dict, estrutura, idioma_conteudo, gemini_generate_fn):
    prompt = f"""Escreva a descrição do vídeo em duas partes, em {idioma_conteudo}.

TESE: {tese_dict['tese']}
OBJEÇÃO: {estrutura['objecao']}
IMPLICAÇÃO: {estrutura['implicacao']}

1. abertura_seo (até 150 caracteres): termos de busca reais que alguém digitaria sobre
   esse tema — não invente termos.
2. corpo: o que o espectador vai aprender e por que a tese é contestável, SEM entregar
   a conclusão do vídeo.

Retorne APENAS JSON: {{"abertura_seo": "...", "corpo": "..."}}"""

    resposta = gemini_generate_fn(prompt)
    return _extrair_json(resposta.text)


# ============================================================
# ORQUESTRAÇÃO — chamada única a partir do generate_video.py
# ============================================================

def gerar_pacote_roteiro(tema, contexto_nicho, idioma_conteudo, instrucao_extra,
                          documento_estilo, tipo_video, gemini_generate_fn,
                          modo_roteiro='cadeia_completa', num_capitulos=3):
    """
    Roda a cadeia completa (modo_roteiro='cadeia_completa', padrão), o modo simples
    sem tese/objeção (modo_roteiro='simples'), ou o modo webdoc em capítulos nomeados
    (modo_roteiro='capitulos_webdoc' — ver gerar_pacote_roteiro_capitulos). Os três
    modos devolvem exatamente o mesmo formato de saída (roteiro_texto, roteiro_blocos,
    titulo, descricao), então nada em generate_video.py precisa saber qual modo rodou —
    inclusive a Fase 2 de produção visual (B-roll por bloco, destaque, SFX) funciona
    igual nos três, porque só depende de roteiro_blocos existir, não de COMO ele foi
    gerado. Se qualquer estágio falhar, cai pra um roteiro simples de emergência (uma
    chamada só) em vez de quebrar o workflow inteiro — mesmo espírito de fallback que
    já existe em criar_audio().
    """
    palavras_alvo = 180 if tipo_video == 'short' else 650

    if modo_roteiro == 'capitulos_webdoc':
        return gerar_pacote_roteiro_capitulos(
            tema, contexto_nicho, idioma_conteudo, instrucao_extra, documento_estilo,
            tipo_video, gemini_generate_fn, num_capitulos=num_capitulos
        )

    try:
        if modo_roteiro == 'simples':
            print("  🧱 Estágio 1/4 — estrutura devocional (sem tese/objeção)...")
            estrutura = gerar_estrutura_devocional(tema, contexto_nicho, idioma_conteudo, gemini_generate_fn)

            print("  ✍️ Estágio 2/4 — escrita...")
            blocos = gerar_prosa(estrutura, contexto_nicho, idioma_conteudo, instrucao_extra,
                                  documento_estilo, palavras_alvo, gemini_generate_fn)

            print("  🔍 Estágio 3/4 — crítica adversarial...")
            blocos = criticar_e_reescrever(blocos, idioma_conteudo, gemini_generate_fn)

            print("  🏷️ Estágio 4/4 — título e descrição...")
            titulo = gerar_titulo_final_simples(tema, estrutura, idioma_conteudo, gemini_generate_fn)
            descricao = gerar_descricao_final_simples(tema, estrutura, idioma_conteudo, gemini_generate_fn)

            roteiro_texto = " ".join(b['texto'] for b in blocos)
            return {
                'roteiro_texto': roteiro_texto,
                'roteiro_blocos': blocos,
                'titulo': titulo,
                'descricao': descricao,
                'tese': None,
                'modo': 'simples',
            }

        print("  🎯 Estágio 1/5 — tese...")
        tese_dict = gerar_tese(tema, contexto_nicho, idioma_conteudo, gemini_generate_fn)
        if tese_dict.get('reprovar'):
            raise ValueError(f"Tese reprovada pelo próprio modelo: {tese_dict.get('tese')}")
        print(f"     tese: {tese_dict['tese']}")

        print("  🧱 Estágio 2/5 — estrutura argumentativa...")
        estrutura = gerar_estrutura(tese_dict, contexto_nicho, idioma_conteudo, gemini_generate_fn)

        print("  ✍️ Estágio 3/5 — escrita...")
        blocos = gerar_prosa(estrutura, contexto_nicho, idioma_conteudo, instrucao_extra,
                              documento_estilo, palavras_alvo, gemini_generate_fn)

        print("  🔍 Estágio 4/5 — crítica adversarial...")
        blocos = criticar_e_reescrever(blocos, idioma_conteudo, gemini_generate_fn)

        print("  🏷️ Estágio 5/5 — título e descrição...")
        titulo = gerar_titulo_final(tese_dict, estrutura, idioma_conteudo, gemini_generate_fn)
        descricao = gerar_descricao_final(tese_dict, estrutura, idioma_conteudo, gemini_generate_fn)

        roteiro_texto = " ".join(b['texto'] for b in blocos)

        return {
            'roteiro_texto': roteiro_texto,
            'roteiro_blocos': blocos,
            'titulo': titulo,
            'descricao': descricao,
            'tese': tese_dict['tese'],
            'modo': 'cadeia_completa',
        }

    except Exception as e:
        print(f"  ⚠️ Cadeia de roteiro falhou ({e}) — usando geração simples de emergência")
        prompt_simples = f"""Crie um roteiro de narração para um vídeo de {contexto_nicho} sobre "{tema}",
em {idioma_conteudo}, ~{palavras_alvo} palavras, tom direto. Escreva APENAS o roteiro corrido."""
        resposta = gemini_generate_fn(prompt_simples)
        roteiro_texto = re.sub(r'\*+', '', resposta.text).replace('#', '').strip()

        return {
            'roteiro_texto': roteiro_texto,
            'roteiro_blocos': [{'bloco': 'roteiro_completo', 'texto': roteiro_texto}],
            'titulo': tema,
            'descricao': {'abertura_seo': tema, 'corpo': roteiro_texto[:200]},
            'tese': None,
            'modo': 'fallback_simples',
        }
