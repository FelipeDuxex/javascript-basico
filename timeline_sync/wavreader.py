"""Leitor de WAV/BWF por leitura parcial.

O Hollyland (e qualquer gravador profissional) grava WAV. O padrao Broadcast
Wave (BWF) guarda data e hora de gravacao no chunk `bext`
(`OriginationDate` + `OriginationTime`), que fica no cabecalho do arquivo — algumas
centenas de bytes. Ou seja: da para saber a hora de um WAV de 2 GB no Drive lendo
1 KB, exatamente como fazemos com os atoms do MP4.

Sem `bext`, tentamos o chunk `iXML` (usado por Zoom/Tascam/Sound Devices) e
depois o `LIST/INFO ICRD`.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional

from .mp4reader import CountingReader, parse_iso_datetime

# Offsets dentro do chunk `bext`, conforme EBU Tech 3285.
BEXT_ORIGINATOR = 256
BEXT_ORIGINATION_DATE = 320   # 10 bytes: YYYY-MM-DD (ou YYYY:MM:DD)
BEXT_ORIGINATION_TIME = 330   # 8 bytes: HH:MM:SS

MAX_HEADER_BYTES = 256 * 1024   # iXML pode ter alguns KB; o resto e ignorado


@dataclass
class WavMetadata:
    path: str = ""
    size: int = 0
    ok: bool = False
    error: str = ""
    time: Optional[datetime] = None
    time_source: str = "none"
    duration: float = 0.0
    sample_rate: int = 0
    channels: int = 0
    bits: int = 0
    tags: Dict[str, str] = field(default_factory=dict)
    bytes_read: int = 0
    reads: int = 0


def _clean(raw: bytes) -> str:
    return raw.decode("utf-8", "ignore").replace("\x00", "").strip()


_IXML_DATE = re.compile(rb"<BWF_ORIGINATION_DATE>([^<]+)<", re.I)
_IXML_TIME = re.compile(rb"<BWF_ORIGINATION_TIME>([^<]+)<", re.I)


def read_metadata(path: str, max_bytes: int = MAX_HEADER_BYTES) -> WavMetadata:
    """Le data/hora e formato de um WAV tocando apenas o cabecalho."""
    meta = WavMetadata(path=path)
    reader: Optional[CountingReader] = None
    try:
        reader = CountingReader(path, max_bytes=max_bytes)
        meta.size = reader.size
        head = reader.read_at(0, 12)
        if len(head) < 12 or head[0:4] not in (b"RIFF", b"RF64") or head[8:12] != b"WAVE":
            meta.error = "nao e um WAV/BWF"
            return meta

        offset = 12
        data_size = 0
        byte_rate = 0
        date_str = ""
        time_str = ""

        while offset + 8 <= reader.size:
            header = reader.read_at(offset, 8)
            if len(header) < 8:
                break
            cid = header[0:4]
            csize = struct.unpack("<I", header[4:8])[0]
            body_at = offset + 8

            if cid == b"fmt ":
                body = reader.read_at(body_at, min(csize, 64))
                if len(body) >= 16:
                    (_fmt, channels, rate, brate, _align, bits) = struct.unpack(
                        "<HHIIHH", body[0:16]
                    )
                    meta.channels = channels
                    meta.sample_rate = rate
                    meta.bits = bits
                    byte_rate = brate
            elif cid == b"data":
                data_size = csize          # o corpo nunca e lido
            elif cid == b"bext":
                body = reader.read_at(body_at, min(csize, 1024))
                if len(body) >= BEXT_ORIGINATION_TIME + 8:
                    date_str = _clean(body[BEXT_ORIGINATION_DATE:BEXT_ORIGINATION_DATE + 10])
                    time_str = _clean(body[BEXT_ORIGINATION_TIME:BEXT_ORIGINATION_TIME + 8])
                    originator = _clean(body[BEXT_ORIGINATOR:BEXT_ORIGINATOR + 32])
                    if originator:
                        meta.tags["make"] = originator
                        meta.tags.setdefault("model", originator)
                if date_str and time_str:
                    meta.time = parse_iso_datetime(f"{date_str}T{time_str}")
                    if meta.time is not None:
                        meta.time_source = "bwf.bext"
            elif cid == b"iXML":
                body = reader.read_at(body_at, min(csize, 32 * 1024))
                if meta.time is None:
                    d = _IXML_DATE.search(body)
                    t = _IXML_TIME.search(body)
                    if d and t:
                        meta.time = parse_iso_datetime(
                            f"{_clean(d.group(1))}T{_clean(t.group(1))}"
                        )
                        if meta.time is not None:
                            meta.time_source = "wav.iXML"
                m = re.search(rb"<PRODUCT>([^<]+)<", body, re.I)
                if m:
                    meta.tags.setdefault("model", _clean(m.group(1)))
            elif cid == b"LIST":
                body = reader.read_at(body_at, min(csize, 4096))
                if body[0:4] == b"INFO":
                    for tag, key in ((b"ICRD", "date"), (b"IPRD", "model"),
                                     (b"IART", "make"), (b"ISFT", "software")):
                        pos = body.find(tag)
                        if pos < 0 or pos + 8 > len(body):
                            continue
                        length = struct.unpack("<I", body[pos + 4:pos + 8])[0]
                        value = _clean(body[pos + 8:pos + 8 + min(length, 128)])
                        if value:
                            meta.tags.setdefault(key, value)
                    if meta.time is None and meta.tags.get("date"):
                        meta.time = parse_iso_datetime(meta.tags["date"])
                        if meta.time is not None:
                            meta.time_source = "wav.INFO"

            offset = body_at + csize + (csize % 2)   # chunks tem padding par
            if csize == 0 and cid not in (b"data",):
                break

        if byte_rate and data_size:
            meta.duration = data_size / byte_rate
        meta.ok = True
    except Exception as exc:
        meta.error = f"{type(exc).__name__}: {exc}"
    finally:
        if reader is not None:
            meta.bytes_read = reader.bytes_read
            meta.reads = reader.reads
            reader.close()
    return meta
