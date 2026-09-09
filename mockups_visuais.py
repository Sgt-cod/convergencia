"""
mockups_visuais.py
-------------------
Fase 3 — mockups visuais procedurais pra uso em webdocs (canais de curiosidade,
história, economia, ciência). NÃO é usado pelo canal de reflexão atual: só entra
em ação quando um bloco do roteiro chega marcado com 'usa_print_noticia' (decidido
em producao_visual.decidir_prints_de_noticia, por sua vez só ativado se o
config.json do canal tiver 'usar_prints_noticia': true).

Gera uma imagem estática simulando o print de uma matéria de site de notícia
genérico — fundo escuro com textura de grade, manchete, autor/data, e um parágrafo
de corpo com um trecho destacado em vermelho, no estilo de card de notícia usado por
canais de webdoc investigativo (design de referência fornecido na conversa em que
esse arquivo foi redesenhado). Nome de veículo é FICTÍCIO por padrão — não usa nome
de veículo real, nem foto de banco de notícia de verdade, nem busca nenhuma notícia
— tudo desenhado do zero. Isso evita de propósito o problema de direito de imagem
que capturar uma manchete de jornal real traria.
"""

import datetime
import os
import random
from PIL import Image, ImageDraw, ImageFont, ImageFilter

_FONTE_HEADLINE_CANDIDATOS = [
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
_FONTE_CORPO_CANDIDATOS = [
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

_NOMES_VEICULO_GENERICOS = [
    "AF News", "Portal Notícia", "Agência Informe", "Diário Central",
    "Rede Informa", "Gazeta Atual",
]

# Paleta do design de referência.
_COR_FUNDO = (20, 19, 21)
_COR_GRADE = (255, 255, 255, 10)          # linhas da grade, bem sutis
_COR_GLOW = (110, 70, 190)                # brilho roxo/azulado no canto inferior esquerdo
_COR_HEADLINE = (255, 255, 255)
_COR_CREDITOS = (150, 150, 155)
_COR_LINHA = (235, 235, 235)
_COR_CORPO = (235, 235, 235)
_COR_DESTAQUE_FUNDO = (176, 30, 38)
_COR_DESTAQUE_TEXTO = (255, 255, 255)


def _carregar_fonte(candidatos, tamanho, arquivo_config=None):
    """arquivo_config: caminho de .ttf vindo do config.json — tem prioridade sobre os
    candidatos padrão do sistema, se o arquivo existir."""
    if arquivo_config and os.path.exists(arquivo_config):
        return ImageFont.truetype(arquivo_config, tamanho)
    for caminho in candidatos:
        if os.path.exists(caminho):
            return ImageFont.truetype(caminho, tamanho)
    print("  ⚠️ Nenhuma fonte TTF encontrada pro print de notícia — usando fonte padrão do PIL")
    return ImageFont.load_default()


def _quebrar_linhas(draw, texto, fonte, largura_max):
    palavras = texto.split()
    linhas, linha_atual = [], ""
    for palavra in palavras:
        teste = f"{linha_atual} {palavra}".strip()
        bbox = draw.textbbox((0, 0), teste, font=fonte)
        if bbox[2] - bbox[0] <= largura_max or not linha_atual:
            linha_atual = teste
        else:
            linhas.append(linha_atual)
            linha_atual = palavra
    if linha_atual:
        linhas.append(linha_atual)
    return linhas


def _desenhar_fundo(largura, altura):
    """Fundo escuro com grade sutil (estilo 'papel quadriculado' escuro) + brilho roxo
    suave no canto inferior esquerdo, igual ao design de referência. Devolve a imagem
    RGB pronta (com fundo já composto) pra continuar desenhando o texto em cima."""
    base = Image.new('RGBA', (largura, altura), (*_COR_FUNDO, 255))

    passo = max(30, int(largura / 27))
    camada_grade = Image.new('RGBA', (largura, altura), (0, 0, 0, 0))
    draw_grade = ImageDraw.Draw(camada_grade)
    for x in range(0, largura, passo):
        draw_grade.line([(x, 0), (x, altura)], fill=_COR_GRADE, width=1)
    for y in range(0, altura, passo):
        draw_grade.line([(0, y), (largura, y)], fill=_COR_GRADE, width=1)
    base = Image.alpha_composite(base, camada_grade)

    raio = int(largura * 0.28)
    cx, cy = -int(raio * 0.35), altura + int(raio * 0.1)
    camada_glow = Image.new('RGBA', (largura, altura), (0, 0, 0, 0))
    draw_glow = ImageDraw.Draw(camada_glow)
    draw_glow.ellipse([cx - raio, cy - raio, cx + raio, cy + raio], fill=(*_COR_GLOW, 90))
    camada_glow = camada_glow.filter(ImageFilter.GaussianBlur(radius=raio * 0.35))
    base = Image.alpha_composite(base, camada_glow)

    return base.convert('RGB')


def _desenhar_paragrafo_com_destaque(draw, corpo, trecho_destaque, fonte, x, y,
                                      largura_max, altura_linha):
    """
    Desenha o parágrafo de corpo quebrado em linhas, com o 'trecho_destaque' (substring
    literal de 'corpo') recebendo um retângulo vermelho atrás do texto — inclusive
    quando o trecho atravessa a quebra de linha (o retângulo é desenhado por LINHA, não
    como um bloco único, senão o destaque "vazaria" por cima de texto de outra linha).
    """
    palavras = corpo.split()
    palavras_destaque = trecho_destaque.split() if trecho_destaque else []
    n_destaque = len(palavras_destaque)

    ini_destaque = fim_destaque = -1
    if n_destaque:
        alvo_normalizado = [p.strip('.,;:!?"').lower() for p in palavras_destaque]
        for i in range(len(palavras) - n_destaque + 1):
            janela = [p.strip('.,;:!?"').lower() for p in palavras[i:i + n_destaque]]
            if janela == alvo_normalizado:
                ini_destaque, fim_destaque = i, i + n_destaque
                break

    linhas, linha_atual, indices_linha_atual = [], "", []
    for idx, palavra in enumerate(palavras):
        teste = f"{linha_atual} {palavra}".strip()
        bbox = draw.textbbox((0, 0), teste, font=fonte)
        if bbox[2] - bbox[0] <= largura_max or not linha_atual:
            linha_atual = teste
            indices_linha_atual.append(idx)
        else:
            linhas.append((linha_atual, indices_linha_atual))
            linha_atual, indices_linha_atual = palavra, [idx]
    if linha_atual:
        linhas.append((linha_atual, indices_linha_atual))

    for texto_linha, indices in linhas:
        indices_destaque_na_linha = [i for i in indices if ini_destaque <= i < fim_destaque]
        if indices_destaque_na_linha:
            palavras_linha = texto_linha.split()
            pos_ini = indices.index(indices_destaque_na_linha[0])
            pos_fim = indices.index(indices_destaque_na_linha[-1]) + 1

            prefixo = " ".join(palavras_linha[:pos_ini])
            trecho_linha = " ".join(palavras_linha[pos_ini:pos_fim])
            largura_espaco = draw.textbbox((0, 0), " ", font=fonte)[2]
            x_ini = x + (draw.textbbox((0, 0), prefixo, font=fonte)[2] + largura_espaco if prefixo else 0)
            bbox_trecho = draw.textbbox((0, 0), trecho_linha, font=fonte)
            largura_trecho = bbox_trecho[2] - bbox_trecho[0]

            pad_v = int(altura_linha * 0.12)
            draw.rectangle(
                [x_ini - 4, y - pad_v, x_ini + largura_trecho + 4, y + altura_linha - pad_v],
                fill=_COR_DESTAQUE_FUNDO
            )
            if prefixo:
                draw.text((x, y), prefixo, font=fonte, fill=_COR_CORPO)
            draw.text((x_ini, y), trecho_linha, font=fonte, fill=_COR_DESTAQUE_TEXTO)
            sufixo = " ".join(palavras_linha[pos_fim:])
            if sufixo:
                x_sufixo = x_ini + largura_trecho + largura_espaco
                draw.text((x_sufixo, y), sufixo, font=fonte, fill=_COR_CORPO)
        else:
            draw.text((x, y), texto_linha, font=fonte, fill=_COR_CORPO)

        y += altura_linha

    return y


def gerar_print_noticia(manchete, corpo=None, trecho_destaque=None, nome_veiculo=None,
                         subtitulo=None, largura=1920, altura=1080,
                         fonte_headline_arquivo=None, fonte_corpo_arquivo=None,
                         output_path='assets/prints/print_noticia.png'):
    """
    Gera o PNG e devolve o caminho salvo. manchete/corpo/trecho_destaque devem vir já
    prontos — quem decide O QUE vira manchete/corpo é
    producao_visual.decidir_prints_de_noticia, baseado no texto real do roteiro; aqui
    só desenha.

    'subtitulo' é aceito só por compatibilidade com chamadas antigas (versão anterior
    do design, que não tinha corpo/trecho_destaque) — se 'corpo' não vier, usa
    'subtitulo' como corpo, sem destaque em vermelho.

    fonte_headline_arquivo/fonte_corpo_arquivo: caminho de .ttf pra sobrescrever a
    fonte padrão do sistema — pensado pra vir de config.json (ex:
    'fonte_print_noticia_headline_arquivo'/'fonte_print_noticia_corpo_arquivo'), do
    mesmo jeito que 'fonte_thumbnail_arquivo' já funciona pra thumbnail.
    """
    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
    nome_veiculo = nome_veiculo or random.choice(_NOMES_VEICULO_GENERICOS)
    corpo = (corpo or subtitulo or "").strip()

    img = _desenhar_fundo(largura, altura)
    draw = ImageDraw.Draw(img)

    margem_x = int(largura * 0.213)
    largura_max = int(largura * 0.615)
    y = int(altura * 0.255)

    # --- manchete: bold branco, até 2-3 linhas ---
    fonte_headline = _carregar_fonte(_FONTE_HEADLINE_CANDIDATOS, int(altura * 0.052),
                                      fonte_headline_arquivo)
    for linha in _quebrar_linhas(draw, manchete, fonte_headline, largura_max)[:3]:
        draw.text((margem_x, y), linha, font=fonte_headline, fill=_COR_HEADLINE)
        y += int(altura * 0.065)

    # --- autor/veículo + data ---
    y += int(altura * 0.035)
    fonte_creditos = _carregar_fonte(_FONTE_CORPO_CANDIDATOS, int(altura * 0.023))
    dias_atras = random.randint(1, 90)
    data_str = (datetime.date.today() - datetime.timedelta(days=dias_atras)).strftime('%d/%m/%Y')
    draw.text((margem_x, y), f"Por: {nome_veiculo}", font=fonte_creditos, fill=_COR_CREDITOS)
    y += int(altura * 0.033)
    draw.text((margem_x, y), f"Publicado: {data_str}", font=fonte_creditos, fill=_COR_CREDITOS)
    y += int(altura * 0.045)

    # --- linha dupla separadora ---
    draw.line([(margem_x, y), (margem_x + largura_max, y)], fill=_COR_LINHA, width=2)
    y += 5
    draw.line([(margem_x, y), (margem_x + largura_max, y)], fill=_COR_LINHA, width=2)
    y += int(altura * 0.035)

    # --- corpo, com trecho em destaque vermelho ---
    if corpo:
        fonte_corpo = _carregar_fonte(_FONTE_CORPO_CANDIDATOS, int(altura * 0.029),
                                       fonte_corpo_arquivo)
        altura_linha = int(altura * 0.048)
        _desenhar_paragrafo_com_destaque(draw, corpo, trecho_destaque or "", fonte_corpo,
                                          margem_x, y, largura_max, altura_linha)

    img.save(output_path, quality=95)
    return output_path
