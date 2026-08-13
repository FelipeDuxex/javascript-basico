"""Injecao de metadado em MP4/MOV — usado apenas para gerar material de teste.

O `ffmpeg` normaliza `creation_time` para UTC e descarta chaves do namespace
Apple (`com.apple.quicktime.model`, `...creationdate`). Como o app precisa ser
validado exatamente contra esses casos — iPhone com fuso explicito, Sony com
relogio literal rotulado Z — escrevemos os atoms na mao aqui.

Nada disso e usado no fluxo normal do app: o app e somente-leitura sobre a
midia. Este modulo existe para `gerar_testes.py`.
"""

from __future__ import annotations

import os
import struct
from typing import Dict, List, Optional, Tuple

from .mp4reader import Atom, iter_atoms_buffer

QT_EPOCH_OFFSET = 2_082_844_800  # segundos entre 1904-01-01 e 1970-01-01


def _atom(atype: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + atype + payload


def _udta_text(atype: bytes, text: str) -> bytes:
    raw = text.encode("utf-8")
    return _atom(atype, struct.pack(">HH", len(raw), 0) + raw)


def _data_utf8(text: str) -> bytes:
    return _atom(b"data", struct.pack(">II", 1, 0) + text.encode("utf-8"))


def build_udta(tags: Dict[bytes, str]) -> bytes:
    """`moov/udta` estilo QuickTime/Sony: atoms (c)mak, (c)mod, (c)day..."""
    payload = b"".join(_udta_text(k, v) for k, v in tags.items() if v)
    return _atom(b"udta", payload)


def build_apple_meta(items: Dict[str, str]) -> bytes:
    """`moov/meta` estilo Apple: hdlr(mdta) + keys + ilst.

    Sem version/flags antes dos filhos — e assim que o iPhone escreve.
    """
    hdlr = _atom(b"hdlr", struct.pack(">I", 0) + b"\x00\x00\x00\x00" + b"mdta"
                 + b"\x00" * 12)
    names = [k for k, v in items.items() if v]
    entries = b"".join(
        struct.pack(">I", 8 + len(n.encode("utf-8"))) + b"mdta" + n.encode("utf-8")
        for n in names
    )
    keys = _atom(b"keys", struct.pack(">II", 0, len(names)) + entries)
    ilst_payload = b"".join(
        _atom(struct.pack(">I", i + 1), _data_utf8(items[n]))
        for i, n in enumerate(names)
    )
    ilst = _atom(b"ilst", ilst_payload)
    return _atom(b"meta", hdlr + keys + ilst)


def _top_level(buf: bytes) -> List[Atom]:
    return list(iter_atoms_buffer(buf))


_OFFSET_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl"}


def _shift_chunk_offsets(moov: bytearray, delta: int, start: int = 8,
                         end: Optional[int] = None) -> None:
    """Corrige `stco`/`co64` depois de mudar o tamanho do `moov`.

    Necessario apenas quando o `moov` fica antes do `mdat` (faststart): crescer
    o `moov` empurra a midia para frente e todos os offsets de chunk mudam.
    """
    if delta == 0:
        return
    if end is None:
        end = len(moov)
    for atom in iter_atoms_buffer(bytes(moov[start:end]), base_offset=start):
        if atom.type in _OFFSET_CONTAINERS:
            _shift_chunk_offsets(moov, delta, atom.offset + atom.header, atom.end)
            continue
        if atom.type not in (b"stco", b"co64"):
            continue
        body = atom.offset + atom.header
        count = struct.unpack(">I", bytes(moov[body + 4:body + 8]))[0]
        wide = atom.type == b"co64"
        step = 8 if wide else 4
        fmt = ">Q" if wide else ">I"
        for k in range(count):
            pos = body + 8 + k * step
            if pos + step > atom.end:
                break
            value = struct.unpack(fmt, bytes(moov[pos:pos + step]))[0]
            struct.pack_into(fmt, moov, pos, value + delta)


def set_mvhd_time(moov: bytearray, epoch_seconds: int) -> None:
    """Reescreve creation/modification time do `mvhd` (segundos desde 1904)."""
    qt = epoch_seconds + QT_EPOCH_OFFSET
    for atom in iter_atoms_buffer(bytes(moov[8:]), base_offset=8):
        if atom.type != b"mvhd":
            continue
        body = atom.offset + atom.header
        version = moov[body]
        if version == 1:
            struct.pack_into(">QQ", moov, body + 4, qt, qt)
        else:
            struct.pack_into(">II", moov, body + 4, qt & 0xFFFFFFFF, qt & 0xFFFFFFFF)
        return


def inject(
    path: str,
    udta_tags: Optional[Dict[bytes, str]] = None,
    apple_items: Optional[Dict[str, str]] = None,
    mvhd_epoch: Optional[int] = None,
) -> None:
    """Reescreve o arquivo com `udta`/`meta` proprios e (opcional) novo `mvhd`.

    Remove `moov/udta` e `moov/meta` preexistentes para nao duplicar chaves.
    """
    with open(path, "rb") as fh:
        buf = fh.read()

    tops = _top_level(buf)
    moov_atom = next((a for a in tops if a.type == b"moov"), None)
    if moov_atom is None:
        raise ValueError(f"{path}: sem atom moov")
    mdat_atom = next((a for a in tops if a.type == b"mdat"), None)

    old_moov = buf[moov_atom.offset:moov_atom.end]
    # remonta os filhos do moov, descartando udta/meta antigos
    kept: List[bytes] = []
    for child in iter_atoms_buffer(old_moov[8:], base_offset=8):
        if child.type in (b"udta", b"meta"):
            continue
        kept.append(old_moov[child.offset:child.offset + child.size])
    extra = b""
    if udta_tags:
        extra += build_udta(udta_tags)
    if apple_items:
        extra += build_apple_meta(apple_items)
    new_payload = b"".join(kept) + extra
    new_moov = bytearray(struct.pack(">I", 8 + len(new_payload)) + b"moov" + new_payload)

    if mvhd_epoch is not None:
        set_mvhd_time(new_moov, mvhd_epoch)

    delta = len(new_moov) - len(old_moov)
    if mdat_atom is not None and moov_atom.offset < mdat_atom.offset:
        _shift_chunk_offsets(new_moov, delta)

    out = buf[:moov_atom.offset] + bytes(new_moov) + buf[moov_atom.end:]
    with open(path, "wb") as fh:
        fh.write(out)


def make_huge_stub(source: str, dest: str, total_size: int) -> Tuple[int, int]:
    """Cria um arquivo esparso grande com estrutura de atoms valida.

    Serve para provar que o leitor nao toca no meio do arquivo: o `mdat` fica
    com gigabytes de zeros esparsos e o `moov` no fim. Retorna
    (offset do moov, tamanho final).
    """
    with open(source, "rb") as fh:
        buf = fh.read()
    tops = _top_level(buf)
    ftyp = next((a for a in tops if a.type == b"ftyp"), None)
    moov = next((a for a in tops if a.type == b"moov"), None)
    if moov is None:
        raise ValueError(f"{source}: sem atom moov")
    head = buf[ftyp.offset:ftyp.end] if ftyp else b""
    moov_bytes = buf[moov.offset:moov.end]

    mdat_size = total_size - len(head) - len(moov_bytes)
    if mdat_size < 16:
        raise ValueError("total_size pequeno demais")
    # mdat com tamanho de 64 bits (size=1 + largesize)
    mdat_header = struct.pack(">I", 1) + b"mdat" + struct.pack(">Q", mdat_size)

    with open(dest, "wb") as fh:
        fh.write(head)
        fh.write(mdat_header)
        # buraco esparso: nada e escrito de fato em disco
        fh.seek(len(head) + mdat_size)
        fh.write(moov_bytes)
    return len(head) + mdat_size, os.path.getsize(dest)
