
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
    texto_falado = aplicar_correcoes_pronuncia(roteiro)
    criar_audio(texto_falado, audio_path)

    # Narração "atropelada" (frases coladas uma na outra): alonga as pausas naturais
    # entre frases pra ~1s (config 'duracao_pausa_frases_ms', default 1000 — 0 desativa).
    # Roda ANTES da transcrição, então todo o resto do pipeline (B-roll, legenda,
    # destaque, SFX) já enxerga os timestamps certos automaticamente.
    duracao_pausa_ms = config.get('duracao_pausa_frases_ms', 1000)
    if duracao_pausa_ms > 0:
        inserir_pausas_entre_frases(audio_path, duracao_pausa_ms=duracao_pausa_ms)

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
        else:
            resultado = criar_video_longo(audio_path, roteiro, lista_clipes, video_path, duracao_narracao,
                                           clips_legenda=clips_legenda, clips_destaque=clips_destaque,
                                           eventos_sfx=eventos_sfx, blocos_com_tempo=blocos_com_tempo)

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