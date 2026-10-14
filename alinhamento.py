"""
alinhamento.py
---------------
Casa as palavras do ROTEIRO (texto certo) com os timestamps do Whisper (tempo certo).

O pipeline original pareia os dois POSICIONALMENTE: a N-ésima palavra do roteiro recebe o
tempo da N-ésima palavra que o Whisper detectou. Em português isso funciona porque as
contagens quase sempre batem. Em norueguês não: o Whisper escreve "nittenhundreogsyttifire"
(1 token) onde o roteiro tem "nitten hundre og syttifire" (4 palavras), junta palavras
compostas, escreve números como dígitos etc. Cada divergência desloca TODAS as palavras
seguintes por uma palavra — depois de algumas dezenas de números, a legenda, os destaques
e o corte de mídia estão segundos fora do lugar.

Aqui o alinhamento é por CONTEÚDO (difflib): as palavras que o Whisper acertou viram
âncoras, e as palavras entre âncoras (as que divergiram) são distribuídas
proporcionalmente ao tamanho dentro da janela de tempo que o Whisper reportou pra aquele
trecho. O erro fica LOCAL (alguns décimos de segundo) em vez de acumular.

Saída: lista com EXATAMENTE len(palavras_roteiro) itens {'inicio','fim'} — o mesmo
contrato que o resto do pipeline já espera, então nada mais precisa mudar.
"""

import re
import unicodedata
from difflib import SequenceMatcher


def _norm(palavra):
    p = unicodedata.normalize('NFC', palavra or '').lower()
    return re.sub(r'[^\w]', '', p, flags=re.UNICODE)


def _distribuir(palavras, t_ini, t_fim):
    """Reparte [t_ini, t_fim] entre as palavras, proporcional ao nº de caracteres."""
    if not palavras:
        return []
    t_fim = max(t_fim, t_ini)
    pesos = [max(1, len(_norm(p))) for p in palavras]
    total = sum(pesos)
    saida, cursor = [], t_ini
    for p, w in zip(palavras, pesos):
        dur = (t_fim - t_ini) * w / total
        saida.append({'inicio': cursor, 'fim': cursor + dur})
        cursor += dur
    return saida


def alinhar_roteiro_com_whisper(palavras_roteiro, palavras_whisper):
    """
    palavras_roteiro: list[str] — texto.split() do roteiro
    palavras_whisper: list[{'inicio','fim','texto'}] — saída do Whisper, com o texto
    Retorna (lista_alinhada, taxa_de_acerto) — taxa = fração das palavras do roteiro que
    casaram EXATAMENTE com o Whisper (diagnóstico: abaixo de ~0.5 vale olhar o áudio).
    """
    n = len(palavras_roteiro)
    if n == 0:
        return [], 1.0
    if not palavras_whisper:
        return [], 0.0

    a = [_norm(w) for w in palavras_roteiro]
    b = [_norm(w['texto']) for w in palavras_whisper]

    resultado = [None] * n
    casadas = 0
    ancoras = []  # (i_roteiro_inicio, j_whisper_inicio, tamanho)
    for i, j, tam in SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks():
        if tam == 0:
            continue
        ancoras.append((i, j, tam))
        for k in range(tam):
            resultado[i + k] = {'inicio': palavras_whisper[j + k]['inicio'],
                                'fim': palavras_whisper[j + k]['fim']}
            casadas += 1

    # Preenche cada "buraco" entre âncoras (e antes da 1ª / depois da última)
    fim_audio = palavras_whisper[-1]['fim']
    limites = [(0, 0, 0)] + ancoras + [(n, len(palavras_whisper), 0)]
    for (i0, j0, t0), (i1, j1, _t1) in zip(limites, limites[1:]):
        gi0, gi1 = i0 + t0, i1          # palavras do roteiro sem âncora
        gj0, gj1 = j0 + t0, j1          # tokens do Whisper sem âncora
        if gi1 <= gi0:
            continue
        if gj1 > gj0:                    # o Whisper ouviu ALGO nesse trecho: usa a janela dele
            t_ini = palavras_whisper[gj0]['inicio']
            t_fim = palavras_whisper[gj1 - 1]['fim']
        else:                            # Whisper não ouviu nada: espreme entre as âncoras
            t_ini = resultado[gi0 - 1]['fim'] if gi0 > 0 and resultado[gi0 - 1] else 0.0
            t_fim = resultado[gi1]['inicio'] if gi1 < n and resultado[gi1] else fim_audio
        for k, slot in enumerate(_distribuir(palavras_roteiro[gi0:gi1], t_ini, t_fim)):
            resultado[gi0 + k] = slot

    # Garantia final: monotônico e sem inversão (inicio <= fim <= próximo inicio)
    anterior_fim = 0.0
    for r in resultado:
        if r['inicio'] < anterior_fim:
            r['inicio'] = anterior_fim
        r['fim'] = max(r['fim'], r['inicio'])
        anterior_fim = r['fim']

    return resultado, casadas / n
