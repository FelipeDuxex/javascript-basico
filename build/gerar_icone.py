#!/usr/bin/env python3
"""Gera `timeline_sync.ico` sem depender de Pillow.

O desenho e o proprio assunto do app: trilhas horizontais com clipes coloridos
sobre fundo roxo escuro, com a coluna dos rotulos a esquerda. As cores sao as
tres primeiras da paleta de blocos (Cerulean, Mango, Forest), entao o icone usa
o mesmo vocabulario visual da interface e da exportacao.

Formato: ICO com entradas BMP (DIB 32bpp BGRA, bottom-up, mais mascara AND).
Escolhi BMP em vez de PNG-dentro-de-ICO porque o empacotador e o Explorer
antigo leem BMP sem ressalva; PNG so vale de Vista pra frente e depende de
quem esta parseando.
"""

from __future__ import annotations

import os
import struct
import sys
from typing import List, Tuple

RGBA = Tuple[int, int, int, int]

# Paleta — os mesmos valores de timeline_sync/colors.py e style.css
FUNDO = (0x1b, 0x17, 0x25, 255)
RAIL = (0x2a, 0x1f, 0x42, 255)
BORDA = (0x45, 0x3a, 0x5e, 255)
CERULEAN = (0x4a, 0x8f, 0xd4, 255)
MANGO = (0xd9, 0x92, 0x2e, 255)
FOREST = (0x54, 0xa0, 0x54, 255)
VAZIO = (0, 0, 0, 0)

# (y inicial, y final, cor, [(x inicial, x final), ...]) — tudo em fracao do lado
TRILHAS: List[Tuple[float, float, RGBA, List[Tuple[float, float]]]] = [
    (0.235, 0.375, CERULEAN, [(0.22, 0.47), (0.53, 0.94)]),
    (0.430, 0.570, MANGO, [(0.22, 0.62), (0.68, 0.90)]),
    (0.625, 0.765, FOREST, [(0.22, 0.39), (0.45, 0.73)]),
]

RAIL_ATE = 0.165        # largura da coluna de rotulos
RAIO = 0.16             # arredondamento do canto, em fracao do lado


def _dentro_do_canto(x: float, y: float, raio: float) -> bool:
    """False nos quatro cantos arredondados (coordenadas 0..1)."""
    for cx, cy in ((raio, raio), (1 - raio, raio),
                   (raio, 1 - raio), (1 - raio, 1 - raio)):
        if ((x < raio and cx == raio) or (x > 1 - raio and cx == 1 - raio)) and \
           ((y < raio and cy == raio) or (y > 1 - raio and cy == 1 - raio)):
            return (x - cx) ** 2 + (y - cy) ** 2 <= raio ** 2
    return True


def pixel(x: float, y: float) -> RGBA:
    """Cor do ponto (x, y), ambos em 0..1."""
    if not _dentro_do_canto(x, y, RAIO):
        return VAZIO
    if x < RAIL_ATE:
        return RAIL
    for y0, y1, cor, segmentos in TRILHAS:
        if y0 <= y <= y1:
            for x0, x1 in segmentos:
                if x0 <= x <= x1:
                    return cor
            # faixa da trilha (o "trilho" vazio entre clipes)
            if 0.20 <= x <= 0.95:
                return BORDA
    return FUNDO


def desenha(lado: int) -> List[List[RGBA]]:
    linhas: List[List[RGBA]] = []
    for j in range(lado):
        linha: List[RGBA] = []
        for i in range(lado):
            # centro do pixel, para a amostragem nao enviesar para a esquerda
            linha.append(pixel((i + 0.5) / lado, (j + 0.5) / lado))
        linhas.append(linha)
    return linhas


def _bmp_dib(pixels: List[List[RGBA]]) -> bytes:
    """DIB 32bpp BGRA, bottom-up, com a mascara AND que o formato ICO exige."""
    lado = len(pixels)
    cabecalho = struct.pack(
        "<IiiHHIIiiII",
        40,             # tamanho do BITMAPINFOHEADER
        lado,           # largura
        lado * 2,       # altura = XOR + AND (exigencia do ICO)
        1,              # planos
        32,             # bits por pixel
        0,              # BI_RGB, sem compressao
        0, 0, 0, 0, 0,
    )
    corpo = bytearray()
    for j in reversed(range(lado)):          # bottom-up
        for r, g, b, a in pixels[j]:
            corpo += bytes((b, g, r, a))     # BGRA
    # Mascara AND: 1 bit por pixel, linhas alinhadas em 4 bytes. Com alfa de
    # verdade no XOR ela fica zerada, mas o formato exige que exista.
    bytes_por_linha = ((lado + 31) // 32) * 4
    mascara = bytes(bytes_por_linha * lado)
    return cabecalho + bytes(corpo) + mascara


def escreve_ico(destino: str, lados: Tuple[int, ...] = (16, 32, 48, 64, 128, 256)) -> str:
    imagens = [(lado, _bmp_dib(desenha(lado))) for lado in lados]
    cabecalho = struct.pack("<HHH", 0, 1, len(imagens))   # reservado, tipo=icone, qtd
    offset = len(cabecalho) + 16 * len(imagens)
    entradas = bytearray()
    dados = bytearray()
    for lado, blob in imagens:
        entradas += struct.pack(
            "<BBBBHHII",
            0 if lado >= 256 else lado,   # 0 significa 256
            0 if lado >= 256 else lado,
            0,      # cores da paleta (0 = sem paleta)
            0,      # reservado
            1,      # planos
            32,     # bits por pixel
            len(blob),
            offset,
        )
        dados += blob
        offset += len(blob)
    os.makedirs(os.path.dirname(os.path.abspath(destino)) or ".", exist_ok=True)
    with open(destino, "wb") as fh:
        fh.write(cabecalho + bytes(entradas) + bytes(dados))
    return destino


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    destino = argv[0] if argv else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "timeline_sync.ico")
    caminho = escreve_ico(destino)
    print(f"icone gerado: {caminho} ({os.path.getsize(caminho) / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
