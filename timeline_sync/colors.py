"""Cores de label do Premiere Pro.

Restricao tecnica: o Premiere usa um conjunto **fixo** de 16 labels nomeadas.
O XML do Final Cut Pro 7 carrega esse nome em `<labels><label2>`; qualquer
valor fora da lista e ignorado na importacao. Ou seja: nao existe "uma cor nova
por bloco" indefinidamente — a estrategia tem que caber em 16 nomes.

Estrategia padrao:

  * a cor representa o BLOCO dentro do dia e a paleta **reinicia a cada dia**;
  * o dispositivo continua identificavel pela posicao vertical (V1, V2, V3...).

Cor = quando. Posicao = qual aparelho. As duas informacoes coexistem sem
conflito, e como cada dia sai em sua propria sequencia nao ha ambiguidade.
Com teto de 6 blocos/dia, as cores nunca acabam.
"""

from __future__ import annotations

from typing import Dict, List

# As 16 labels do Premiere. Os hex sao aproximacoes usadas na previa do app —
# o Premiere pinta com os seus proprios valores, o nome e o que importa.
PREMIERE_LABELS: Dict[str, str] = {
    "Violet":    "#8f7fc4",
    "Iris":      "#6f6bc8",
    "Caribbean": "#3f9e93",
    "Lavender":  "#b394d0",
    "Cerulean":  "#4a8fd4",
    "Forest":    "#3f7a45",
    "Rose":      "#d97f96",
    "Mango":     "#d9922e",
    "Purple":    "#7a4fb5",
    "Blue":      "#3f5fa8",
    "Teal":      "#2e8f8f",
    "Magenta":   "#b03f8c",
    "Tan":       "#bd9a6a",
    "Green":     "#54a054",
    "Brown":     "#8a6444",
    "Yellow":    "#c9bf3f",
}

VALID_LABELS = tuple(PREMIERE_LABELS)

# Paleta de blocos: 6 cores bem separadas entre si e contra o fundo escuro.
# A ordem garante que blocos consecutivos nunca compartilhem cor.
BLOCK_PALETTE: List[str] = [
    "Cerulean",   # azul
    "Mango",      # laranja
    "Forest",     # verde escuro
    "Rose",       # rosa
    "Iris",       # roxo azulado
    "Caribbean",  # ciano
]

# Paleta de dias (usada na sequencia MASTER e no modo "colorir por dia").
DAY_PALETTE: List[str] = [
    "Violet", "Teal", "Tan", "Blue", "Green", "Magenta", "Yellow",
]

# Paleta de dispositivos (modo classico de conferencia).
DEVICE_PALETTE: List[str] = [
    "Blue", "Mango", "Green", "Magenta", "Cerulean", "Tan", "Rose", "Teal",
    "Forest", "Violet", "Yellow", "Brown", "Purple", "Iris", "Lavender",
    "Caribbean",
]


def is_valid(label: str) -> bool:
    return label in PREMIERE_LABELS


def hex_for(label: str) -> str:
    return PREMIERE_LABELS.get(label, "#7a7a7a")


def block_label(block_index: int) -> str:
    """Cor do bloco N do dia. `block_index` e 1-based e reinicia a cada dia."""
    return BLOCK_PALETTE[(max(block_index, 1) - 1) % len(BLOCK_PALETTE)]


def day_label(day_index: int) -> str:
    return DAY_PALETTE[(max(day_index, 1) - 1) % len(DAY_PALETTE)]


def device_label(track_order: int) -> str:
    return DEVICE_PALETTE[max(track_order, 0) % len(DEVICE_PALETTE)]


def label_for(mode: str, day_index: int, block_index: int,
              device_order: int) -> str:
    """Cor de um clipe conforme o modo escolhido.

    A previa na tela usa exatamente esta funcao, para o que aparece no app
    bater com o que vai aparecer no Premiere.
    """
    if mode == "dia":
        return day_label(day_index)
    if mode == "dispositivo":
        return device_label(device_order)
    return block_label(block_index)


def palette_for(mode: str) -> List[str]:
    if mode == "dia":
        return list(DAY_PALETTE)
    if mode == "dispositivo":
        return list(DEVICE_PALETTE)
    return list(BLOCK_PALETTE)
