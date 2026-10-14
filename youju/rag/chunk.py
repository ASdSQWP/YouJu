"""按章节切片。同一章节内按行累积到 size，换块时带上尾部若干整行作为重叠。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from youju.rag.parse import Block

_SENTENCE = re.compile(r"[^。！？!?；;]+[。！？!?；;]*")


@dataclass
class Draft:
    text: str
    section: str
    page: int | None


def _units(text: str, size: int) -> list[str]:
    units: list[str] = []
    for line in text.splitlines():
        line = line.rstrip()
        if not line.strip():
            continue
        if len(line) <= size:
            units.append(line)
            continue
        for piece in (m.group(0) for m in _SENTENCE.finditer(line)):
            while len(piece) > size:
                units.append(piece[:size])
                piece = piece[size:]
            if piece.strip():
                units.append(piece)
    return units


def _tail(units: list[str], overlap: int) -> list[str]:
    kept: list[str] = []
    total = 0
    for unit in reversed(units):
        if total + len(unit) > overlap:
            break
        kept.insert(0, unit)
        total += len(unit) + 1
    return kept


def chunk_blocks(blocks: list[Block], size: int = 500, overlap: int = 80) -> list[Draft]:
    size = max(120, int(size))
    overlap = max(0, min(int(overlap), size // 2))
    drafts: list[Draft] = []
    group: list[str] = []
    length = 0
    key: tuple[str, int | None] | None = None

    def emit() -> None:
        nonlocal group, length
        if group and key is not None:
            drafts.append(Draft("\n".join(group), key[0], key[1]))
        group, length = [], 0

    for block in blocks:
        block_key = (block.section, block.page)
        if key is not None and block_key != key:
            emit()
        key = block_key
        for unit in _units(block.text, size):
            if group and length + len(unit) + 1 > size:
                carried = _tail(group, overlap)
                emit()
                group = list(carried)
                length = sum(len(u) + 1 for u in group)
            group.append(unit)
            length += len(unit) + 1
    emit()
    return drafts
