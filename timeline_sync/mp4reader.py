"""Leitor de metadados MP4/MOV por leitura parcial (byte-range).

Motivacao: o material fica no Google Drive e e aberto em modo streaming. Ler um
arquivo inteiro so para descobrir a hora de gravacao faria o Drive baixar
gigabytes por clipe. Este modulo percorre a arvore de atoms com `seek()` e le
apenas os poucos KB que realmente importam (`moov/mvhd`, `moov/udta`,
`moov/meta`, cabecalhos de trilha), jamais tocando no `mdat` nem nas tabelas de
amostra (`stco`, `stsz`, `stts`...), que sao as partes grandes do `moov`.

Todo byte lido passa por `CountingReader`, que contabiliza o volume real de
I/O — e isso vai para o relatorio final ("14 MB transferidos de 340 GB").
"""

from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterator, List, Optional, Tuple

# Epoca do QuickTime: 1904-01-01 00:00:00 UTC.
QT_EPOCH = datetime(1904, 1, 1, tzinfo=timezone.utc)

# Atoms que sao containers e podem ser percorridos recursivamente.
CONTAINER_ATOMS = {
    b"moov", b"trak", b"mdia", b"minf", b"stbl", b"udta", b"meta",
    b"ilst", b"edts", b"mvex", b"dinf",
}

# Atoms grandes que NUNCA devem ser lidos: sao tabelas de amostra ou midia.
SKIP_ATOMS = {
    b"mdat", b"free", b"skip", b"wide", b"stco", b"co64", b"stsz", b"stz2",
    b"stts", b"ctts", b"stsc", b"stss", b"sdtp", b"sbgp", b"sgpd", b"subs",
    b"saiz", b"saio", b"cslg",
}

# Tipos de handler que sao MIDIA (e nao data handler como 'alis'/'url ').
MEDIA_HANDLERS = {"vide", "soun", "tmcd", "text", "sbtl", "subp", "clcp",
                  "meta", "hint", "mdta"}

# Se um container couber nesse limite, e lido de uma vez (1 seek + 1 read).
# Acima disso, e percorrido por seek para nao arrastar tabelas de amostra.
INLINE_LIMIT = 512 * 1024

# Teto absoluto de bytes lidos por arquivo. Uma travessia normal usa 10-60 KB.
DEFAULT_MAX_BYTES = 4 * 1024 * 1024

# udta 4cc -> nome logico. O prefixo 0xA9 e o "(c)" do QuickTime.
UDTA_TAGS = {
    b"\xa9mak": "make",
    b"\xa9mod": "model",
    b"\xa9swr": "software",
    b"\xa9day": "date",
    b"\xa9nam": "title",
    b"\xa9cmt": "comment",
    b"\xa9enc": "encoder",
    b"\xa9xyz": "location",
    b"manu": "make",
    b"modl": "model",
}

# Chaves do namespace Apple (moov/meta/keys) que interessam.
APPLE_KEYS = {
    "com.apple.quicktime.model": "model",
    "com.apple.quicktime.make": "make",
    "com.apple.quicktime.software": "software",
    "com.apple.quicktime.creationdate": "apple_creationdate",
    "com.apple.quicktime.location.ISO6709": "location",
    "com.apple.proapps.clipID": "clip_id",
}


class TooManyBytes(Exception):
    """Levantada quando a travessia estoura o teto de bytes do arquivo."""


class CountingReader:
    """Acesso por offset com contabilidade de bytes e teto de leitura."""

    def __init__(self, path: str, max_bytes: int = DEFAULT_MAX_BYTES):
        self.path = path
        self.size = os.path.getsize(path)
        self.max_bytes = max_bytes
        self.bytes_read = 0
        self.reads = 0
        self._fh = open(path, "rb")

    def read_at(self, offset: int, length: int) -> bytes:
        if length <= 0 or offset >= self.size:
            return b""
        length = min(length, self.size - offset)
        if self.bytes_read + length > self.max_bytes:
            raise TooManyBytes(
                f"leitura excederia {self.max_bytes} bytes em {self.path}"
            )
        self._fh.seek(offset)
        data = self._fh.read(length)
        self.bytes_read += len(data)
        self.reads += 1
        return data

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass

    def __enter__(self) -> "CountingReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


@dataclass
class Atom:
    type: bytes
    offset: int          # offset do inicio do atom (do campo de tamanho)
    size: int            # tamanho total, incluindo cabecalho
    header: int          # 8 ou 16 bytes

    @property
    def body_offset(self) -> int:
        return self.offset + self.header

    @property
    def body_size(self) -> int:
        return self.size - self.header

    @property
    def end(self) -> int:
        return self.offset + self.size


@dataclass
class TrackInfo:
    kind: str = "unknown"      # vide | soun | tmcd | ...
    codec: str = ""
    width: int = 0
    height: int = 0
    duration: float = 0.0
    sample_rate: int = 0
    channels: int = 0


@dataclass
class Mp4Metadata:
    """Resultado da leitura de um arquivo MP4/MOV."""

    path: str = ""
    size: int = 0
    ok: bool = False
    error: str = ""
    major_brand: str = ""
    # mvhd guarda um instante "rotulado" como UTC, mas cameras frequentemente
    # escrevem ali o valor literal do relogio interno. Guardamos os dois usos.
    mvhd_time_utc: Optional[datetime] = None
    mvhd_time_literal: Optional[datetime] = None   # mesmo valor, naive
    duration: float = 0.0
    tags: Dict[str, str] = field(default_factory=dict)
    tracks: List[TrackInfo] = field(default_factory=list)
    bytes_read: int = 0
    reads: int = 0
    moov_at_start: bool = False

    # --- conveniencias -------------------------------------------------
    @property
    def has_video(self) -> bool:
        return any(t.kind == "vide" for t in self.tracks)

    @property
    def has_audio(self) -> bool:
        return any(t.kind == "soun" for t in self.tracks)

    @property
    def video_track(self) -> Optional[TrackInfo]:
        for t in self.tracks:
            if t.kind == "vide":
                return t
        return None

    @property
    def audio_track(self) -> Optional[TrackInfo]:
        for t in self.tracks:
            if t.kind == "soun":
                return t
        return None

    def best_time(self) -> Tuple[Optional[datetime], str, bool]:
        """Melhor instante de gravacao disponivel.

        Retorna (datetime, fonte, timezone_conhecido).

        Prioridade:
        1. `com.apple.quicktime.creationdate` — traz offset de fuso explicito,
           e o iPhone acerta a hora sozinho. Fonte mais confiavel que existe.
        2. `udta/(c)day` quando trouxer offset de fuso explicito.
        3. `mvhd` — valor literal do relogio, sem fuso confiavel.
        4. `udta/(c)day` sem fuso.
        """
        apple = self.tags.get("apple_creationdate")
        if apple:
            dt = parse_iso_datetime(apple)
            if dt is not None:
                return dt, "quicktime.creationdate", dt.tzinfo is not None
        day = self.tags.get("date")
        day_dt = parse_iso_datetime(day) if day else None
        if day_dt is not None and day_dt.tzinfo is not None:
            return day_dt, "udta.day", True
        if self.mvhd_time_literal is not None:
            return self.mvhd_time_literal, "mvhd", False
        if day_dt is not None:
            return day_dt, "udta.day", False
        return None, "none", False


# ----------------------------------------------------------------------
# parsing de datas
# ----------------------------------------------------------------------

_ISO_RE = re.compile(
    r"(?P<y>\d{4})[-:]?(?P<mo>\d{2})[-:]?(?P<d>\d{2})"
    r"[T ](?P<h>\d{2}):?(?P<mi>\d{2}):?(?P<s>\d{2})"
    r"(?P<frac>\.\d+)?"
    r"(?P<tz>Z|[+-]\d{2}:?\d{2})?"
)


def parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    """Parser tolerante de datas ISO-ish vindas de metadado de camera.

    Aceita `2026-08-13T00:47:01-0300`, `2026:08:13 00:47:01`, `...Z`, etc.
    """
    if not value:
        return None
    m = _ISO_RE.search(value.strip())
    if not m:
        return None
    micro = 0
    if m.group("frac"):
        micro = int(round(float(m.group("frac")) * 1_000_000))
        micro = min(micro, 999_999)
    try:
        dt = datetime(
            int(m.group("y")), int(m.group("mo")), int(m.group("d")),
            int(m.group("h")), int(m.group("mi")), int(m.group("s")), micro,
        )
    except ValueError:
        return None
    tz = m.group("tz")
    if tz:
        if tz == "Z":
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            tz = tz.replace(":", "")
            sign = 1 if tz[0] == "+" else -1
            delta = timedelta(hours=int(tz[1:3]), minutes=int(tz[3:5]))
            dt = dt.replace(tzinfo=timezone(sign * delta))
    return dt


def _qt_time(seconds: int) -> Optional[datetime]:
    if seconds <= 0:
        return None
    try:
        return QT_EPOCH + timedelta(seconds=seconds)
    except OverflowError:
        return None


# ----------------------------------------------------------------------
# travessia de atoms
# ----------------------------------------------------------------------

def iter_atoms_seek(reader: CountingReader, start: int, end: int) -> Iterator[Atom]:
    """Percorre atoms lendo apenas o cabecalho de cada um (8-16 bytes)."""
    off = start
    while off + 8 <= end:
        hdr = reader.read_at(off, 16)
        if len(hdr) < 8:
            return
        size = struct.unpack(">I", hdr[0:4])[0]
        atype = hdr[4:8]
        header = 8
        if size == 1:
            if len(hdr) < 16:
                return
            size = struct.unpack(">Q", hdr[8:16])[0]
            header = 16
        elif size == 0:
            size = end - off
        if size < header or off + size > end + 8:
            return
        yield Atom(atype, off, min(size, end - off), header)
        off += size


def iter_atoms_buffer(buf: bytes, base_offset: int = 0) -> Iterator[Atom]:
    """Percorre atoms de um buffer ja em memoria (nenhum I/O adicional)."""
    off = 0
    n = len(buf)
    while off + 8 <= n:
        size = struct.unpack(">I", buf[off:off + 4])[0]
        atype = buf[off + 4:off + 8]
        header = 8
        if size == 1:
            if off + 16 > n:
                return
            size = struct.unpack(">Q", buf[off + 8:off + 16])[0]
            header = 16
        elif size == 0:
            size = n - off
        if size < header:
            return
        yield Atom(atype, base_offset + off, min(size, n - off), header)
        off += size


class _Walker:
    """Percorre o `moov` coletando so o que interessa.

    Containers pequenos sao lidos de uma vez (buffer). Containers grandes
    (`moov` de clipe longo, `stbl`) sao percorridos por seek, e os atoms
    listados em SKIP_ATOMS nunca sao lidos.
    """

    def __init__(self, reader: CountingReader, meta: Mp4Metadata):
        self.r = reader
        self.meta = meta
        self._track: Optional[TrackInfo] = None
        self._movie_timescale = 0

    # -- entrada --------------------------------------------------------
    def walk_moov(self, moov: Atom) -> None:
        self._walk_container(moov, depth=0)

    def _walk_container(self, atom: Atom, depth: int) -> None:
        if depth > 6:
            return
        if atom.body_size <= INLINE_LIMIT:
            buf = self.r.read_at(atom.body_offset, atom.body_size)
            children = list(iter_atoms_buffer(buf, atom.body_offset))
            for child in children:
                rel = child.offset - atom.body_offset
                body = buf[rel + child.header:rel + child.size]
                self._handle(child, depth + 1, body)
        else:
            for child in iter_atoms_seek(self.r, atom.body_offset, atom.end):
                self._handle(child, depth + 1, None)

    def _handle(self, atom: Atom, depth: int, body: Optional[bytes]) -> None:
        t = atom.type
        if t in SKIP_ATOMS:
            return
        if t == b"trak":
            self._track = TrackInfo()
            self.meta.tracks.append(self._track)
            self._walk_or_reuse(atom, depth, body)
            self._track = None
            return
        if t in CONTAINER_ATOMS:
            if t == b"meta":
                self._parse_meta(atom, body)
                return
            if t == b"udta":
                self._parse_udta(atom, body)
                return
            self._walk_or_reuse(atom, depth, body)
            return
        # folhas de interesse
        if t == b"mvhd":
            self._parse_mvhd(self._body(atom, body))
        elif t == b"tkhd":
            self._parse_tkhd(self._body(atom, body))
        elif t == b"mdhd":
            self._parse_mdhd(self._body(atom, body))
        elif t == b"hdlr":
            self._parse_hdlr(self._body(atom, body))
        elif t == b"stsd":
            self._parse_stsd(self._body(atom, body))

    def _walk_or_reuse(self, atom: Atom, depth: int, body: Optional[bytes]) -> None:
        if body is not None:
            for child in iter_atoms_buffer(body, atom.body_offset):
                rel = child.offset - atom.body_offset
                self._handle(child, depth + 1, body[rel + child.header:rel + child.size])
        else:
            self._walk_container(atom, depth)

    def _body(self, atom: Atom, body: Optional[bytes]) -> bytes:
        if body is not None:
            return body
        # folhas de interesse sao pequenas; 4 KB cobre todas com folga.
        return self.r.read_at(atom.body_offset, min(atom.body_size, 4096))

    # -- folhas ---------------------------------------------------------
    def _parse_mvhd(self, b: bytes) -> None:
        if len(b) < 4:
            return
        version = b[0]
        try:
            if version == 1:
                created, _modified, timescale, duration = struct.unpack(
                    ">QQIQ", b[4:32]
                )
            else:
                created, _modified, timescale, duration = struct.unpack(
                    ">IIII", b[4:20]
                )
        except struct.error:
            return
        self._movie_timescale = timescale or 0
        dt = _qt_time(created)
        if dt is not None:
            self.meta.mvhd_time_utc = dt
            self.meta.mvhd_time_literal = dt.replace(tzinfo=None)
        if timescale:
            self.meta.duration = duration / timescale

    def _parse_tkhd(self, b: bytes) -> None:
        if self._track is None or len(b) < 4:
            return
        version = b[0]
        off = 84 if version == 1 else 76
        if len(b) >= off + 8:
            w, h = struct.unpack(">II", b[off:off + 8])
            self._track.width = w >> 16
            self._track.height = h >> 16

    def _parse_mdhd(self, b: bytes) -> None:
        if self._track is None or len(b) < 4:
            return
        version = b[0]
        try:
            if version == 1:
                timescale, duration = struct.unpack(">IQ", b[20:32])
            else:
                timescale, duration = struct.unpack(">II", b[12:20])
        except struct.error:
            return
        if timescale:
            self._track.duration = duration / timescale

    def _parse_hdlr(self, b: bytes) -> None:
        if self._track is None or len(b) < 12:
            return
        try:
            subtype = b[8:12].decode("ascii").strip()
        except UnicodeDecodeError:
            return
        # `minf` tambem tem um `hdlr` (o data handler: 'alis', 'url '). So
        # aceitamos tipos de MIDIA, senao ele sobrescreve o do `mdia`.
        if subtype in MEDIA_HANDLERS:
            self._track.kind = subtype

    def _parse_stsd(self, b: bytes) -> None:
        if self._track is None or len(b) < 16:
            return
        for entry in iter_atoms_buffer(b[8:]):
            try:
                self._track.codec = entry.type.decode("ascii").strip()
            except UnicodeDecodeError:
                self._track.codec = repr(entry.type)
            rel = entry.offset - 0
            body = b[8 + rel + entry.header: 8 + rel + entry.size]
            # Layout de uma sample entry de audio, apos os 8 bytes de
            # reserved+data_reference_index: version/revision/vendor (8),
            # channelcount em 16, samplerate (16.16 fixo) em 24.
            if self._track.kind == "soun" and len(body) >= 28:
                try:
                    self._track.channels = struct.unpack(">H", body[16:18])[0]
                    self._track.sample_rate = struct.unpack(">I", body[24:28])[0] >> 16
                except struct.error:
                    pass
            elif self._track.kind == "vide" and len(body) >= 32:
                try:
                    w, h = struct.unpack(">HH", body[24:28])
                    if w and h:
                        self._track.width = self._track.width or w
                        self._track.height = self._track.height or h
                except struct.error:
                    pass
            break

    # -- udta / meta ----------------------------------------------------
    def _parse_udta(self, atom: Atom, body: Optional[bytes]) -> None:
        if body is None:
            body = self.r.read_at(atom.body_offset, min(atom.body_size, INLINE_LIMIT))
        for child in iter_atoms_buffer(body, atom.body_offset):
            rel = child.offset - atom.body_offset
            cbody = body[rel + child.header:rel + child.size]
            if child.type == b"meta":
                self._parse_meta(child, cbody)
                continue
            name = UDTA_TAGS.get(child.type)
            if not name:
                continue
            value = _decode_udta_value(cbody)
            if value:
                self.meta.tags.setdefault(name, value)

    def _parse_meta(self, atom: Atom, body: Optional[bytes]) -> None:
        if body is None:
            body = self.r.read_at(atom.body_offset, min(atom.body_size, INLINE_LIMIT))
        if not body:
            return
        # `meta` costuma ter 4 bytes de version/flags antes dos filhos; alguns
        # arquivos QuickTime nao tem. Detectamos olhando o primeiro atom.
        offsets = [0, 4]
        keys: List[str] = []
        items: Dict[int, bytes] = {}
        for skip in offsets:
            found = False
            for child in iter_atoms_buffer(body[skip:], atom.body_offset + skip):
                if child.type in (b"hdlr", b"keys", b"ilst", b"XMP_", b"mdta"):
                    found = True
                    break
            if not found:
                continue
            for child in iter_atoms_buffer(body[skip:], atom.body_offset + skip):
                rel = child.offset - (atom.body_offset + skip)
                cbody = body[skip + rel + child.header: skip + rel + child.size]
                if child.type == b"keys":
                    keys = _parse_keys(cbody)
                elif child.type == b"ilst":
                    items.update(_parse_ilst(cbody))
                elif child.type == b"udta":
                    self._parse_udta(child, cbody)
            break
        # nomes Apple (via keys) tem prioridade; itunes-style usa 4cc.
        for index, raw in items.items():
            value = _decode_data_atom(raw)
            if not value:
                continue
            if 1 <= index <= len(keys):
                key = keys[index - 1]
                logical = APPLE_KEYS.get(key)
                if logical:
                    self.meta.tags[logical] = value
                else:
                    self.meta.tags.setdefault(key, value)


def _decode_udta_value(body: bytes) -> str:
    """Decodifica o payload de um atom udta estilo QuickTime.

    Layout classico: uint16 tamanho + uint16 idioma + texto. Alguns arquivos
    usam o layout iTunes (`data` interno). Tentamos os dois.
    """
    if not body:
        return ""
    for child in iter_atoms_buffer(body):
        if child.type == b"data":
            return _decode_data_atom(body[child.header:child.size])
    if len(body) >= 4:
        size = struct.unpack(">H", body[0:2])[0]
        if 0 < size <= len(body) - 4:
            return _clean_text(body[4:4 + size])
    return _clean_text(body)


def _decode_data_atom(body: bytes) -> str:
    """Payload de um atom `data`: uint32 type + uint32 locale + valor."""
    if len(body) < 8:
        return _clean_text(body)
    dtype = struct.unpack(">I", body[0:4])[0] & 0x00FFFFFF
    payload = body[8:]
    if dtype in (1, 0):  # UTF-8 / binario tratado como texto
        return _clean_text(payload)
    if dtype == 2:
        try:
            return payload.decode("utf-16-be").strip("\x00").strip()
        except UnicodeDecodeError:
            return ""
    if dtype in (21, 22) and payload:  # inteiro assinado / sem sinal
        return str(int.from_bytes(payload, "big", signed=(dtype == 21)))
    return _clean_text(payload)


def _clean_text(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8", errors="ignore")
    except Exception:
        return ""
    return text.replace("\x00", "").strip()


def _parse_keys(body: bytes) -> List[str]:
    """`keys` = version/flags + count + N entradas (size, namespace, nome)."""
    keys: List[str] = []
    if len(body) < 8:
        return keys
    count = struct.unpack(">I", body[4:8])[0]
    off = 8
    for _ in range(min(count, 512)):
        if off + 8 > len(body):
            break
        size = struct.unpack(">I", body[off:off + 4])[0]
        if size < 8 or off + size > len(body):
            break
        keys.append(_clean_text(body[off + 8:off + size]))
        off += size
    return keys


def _parse_ilst(body: bytes) -> Dict[int, bytes]:
    """`ilst` = lista de atoms cujo *tipo* e o indice (1-based) em `keys`."""
    items: Dict[int, bytes] = {}
    for child in iter_atoms_buffer(body):
        index = struct.unpack(">I", child.type)[0]
        payload = body[child.offset + child.header: child.offset + child.size]
        for sub in iter_atoms_buffer(payload):
            if sub.type == b"data":
                items[index] = payload[sub.offset + sub.header: sub.offset + sub.size]
                break
        else:
            items[index] = payload
    return items


# ----------------------------------------------------------------------
# API publica
# ----------------------------------------------------------------------

def read_metadata(path: str, max_bytes: int = DEFAULT_MAX_BYTES) -> Mp4Metadata:
    """Le metadados de um MP4/MOV tocando o minimo possivel do arquivo.

    O `moov` pode estar no inicio (faststart) ou no fim. Se nao estiver entre
    os primeiros atoms, saltamos direto para o fim do arquivo — sem nunca ler
    o meio (o `mdat`).
    """
    meta = Mp4Metadata(path=path)
    reader = None
    try:
        reader = CountingReader(path, max_bytes=max_bytes)
        meta.size = reader.size
        moov: Optional[Atom] = None
        seen = 0
        for atom in iter_atoms_seek(reader, 0, reader.size):
            seen += 1
            if atom.type == b"ftyp":
                head = reader.read_at(atom.body_offset, min(8, atom.body_size))
                meta.major_brand = _clean_text(head[:4])
            elif atom.type == b"moov":
                moov = atom
                meta.moov_at_start = True
                break
            elif atom.type == b"mdat":
                # o moov vem depois da midia: continuamos a travessia, que
                # apenas salta o mdat (nenhum byte dele e lido).
                continue
            if seen > 64:
                break
        if moov is None:
            meta.error = "atom moov nao encontrado"
        else:
            walker = _Walker(reader, meta)
            walker.walk_moov(moov)
            meta.ok = True
    except TooManyBytes as exc:
        meta.error = str(exc)
    except (OSError, struct.error, ValueError) as exc:
        meta.error = f"{type(exc).__name__}: {exc}"
    finally:
        if reader is not None:
            meta.bytes_read = reader.bytes_read
            meta.reads = reader.reads
            reader.close()
    return meta
