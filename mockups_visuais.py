"""
mockups_visuais.py
-------------------
Fase 3 — mockups visuais procedurais pra uso em webdocs (canais de curiosidade,
história, economia, ciência). NÃO é usado pelo canal de reflexão atual: só entra
em ação quando um bloco do roteiro chega marcado com 'usa_print_noticia' (decidido
em producao_visual.decidir_prints_de_noticia, por sua vez só ativado se o
config.json do canal tiver 'usar_prints_noticia': true).

Gera uma imagem estática simulando o print de um site de notícia genérico — barra
de navegador falsa, nome de veículo FICTÍCIO, manchete e subtítulo com o texto
que o Gemini já extraiu do próprio roteiro. Não usa nome de veículo real, nem foto
de banco de notícia de verdade, nem busca nenhuma notícia — tudo desenhado do zero.
Isso evita de propósito o problema de direito de imagem que capturar uma manchete
de jornal real traria, ao custo de não ser um print "autêntico" — é uma ilustração,
não uma citação de fonte.
"""

import os
import random
from PIL import Image, ImageDraw, ImageFont, ImageFilter

_FONTE_SERIF_CANDIDATOS = [
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
]
_FONTE_SANS_CANDIDATOS = [
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

_NOMES_VEICULO_GENERICOS = [
    "JORNAL DO DIA", "PORTAL NOTÍCIA", "AGÊNCIA INFORME", "DIÁRIO CENTRAL",
    "REDE INFORMA", "GAZETA ATUAL",
]


def _carregar_fonte(candidatos, tamanho):
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


def gerar_print_noticia(manchete, subtitulo=None, nome_veiculo=None,
                         largura=1920, altura=1080,
                         output_path='assets/prints/print_noticia.png'):
    """
    Gera o PNG e devolve o caminho salvo. manchete/subtitulo devem vir já prontos
    (curtos, sem quebras de linha) — quem decide O QUE vira manchete é
    producao_visual.decidir_prints_de_noticia, baseado no texto real do roteiro;
    aqui só desenha.
    """
    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
    nome_veiculo = nome_veiculo or random.choice(_NOMES_VEICULO_GENERICOS)

    img = Image.new('RGB', (largura, altura), color=(252, 252, 250))
    draw = ImageDraw.Draw(img)

    # --- barra de navegador falsa (topo), pra dar contexto de "isso é uma tela") ---
    altura_barra = int(altura * 0.06)
    draw.rectangle([0, 0, largura, altura_barra], fill=(230, 230, 230))
    for i, cor in enumerate([(255, 95, 87), (255, 189, 46), (39, 201, 63)]):
        cx = int(altura_barra * 0.5) + i * int(altura_barra * 0.6)
        r = int(altura_barra * 0.16)
        cy = altura_barra // 2
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=cor)
    draw.rounded_rectangle(
        [int(largura * 0.15), int(altura_barra * 0.2), int(largura * 0.6), int(altura_barra * 0.8)],
        radius=int(altura_barra * 0.25), fill=(255, 255, 255)
    )

    # --- cabeçalho do "site" ---
    y = altura_barra + int(altura * 0.03)
    fonte_logo = _carregar_fonte(_FONTE_SERIF_CANDIDATOS, int(altura * 0.05))
    draw.text((int(largura * 0.05), y), nome_veiculo, font=fonte_logo, fill=(20, 20, 20))
    y += int(altura * 0.09)
    draw.line([(int(largura * 0.05), y), (int(largura * 0.95), y)], fill=(210, 210, 210), width=3)
    y += int(altura * 0.05)

    # --- manchete ---
    fonte_manchete = _carregar_fonte(_FONTE_SERIF_CANDIDATOS, int(altura * 0.075))
    largura_max = int(largura * 0.9)
    for linha in _quebrar_linhas(draw, manchete.upper(), fonte_manchete, largura_max):
        draw.text((int(largura * 0.05), y), linha, font=fonte_manchete, fill=(10, 10, 10))
        y += int(altura * 0.09)

    # --- subtítulo ---
    if subtitulo:
        y += int(altura * 0.02)
        fonte_sub = _carregar_fonte(_FONTE_SANS_CANDIDATOS, int(altura * 0.035))
        for linha in _quebrar_linhas(draw, subtitulo, fonte_sub, largura_max)[:3]:
            draw.text((int(largura * 0.05), y), linha, font=fonte_sub, fill=(80, 80, 80))
            y += int(altura * 0.05)

    # leve suavização pra não parecer um slide de apresentação
    img = img.filter(ImageFilter.SMOOTH_MORE)
    img.save(output_path, quality=95)
    return output_path
