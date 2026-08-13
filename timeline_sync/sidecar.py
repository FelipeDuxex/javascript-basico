"""Leitura de arquivos sidecar (XML ao lado do clipe).

Camera Sony (XAVC S) grava um XML minusculo junto de cada clipe — poucos KB
contra os gigabytes do video. Se ele existe, a data sai dali e o arquivo de
video nunca e aberto. Esse e o caminho mais rapido possivel no Google Drive.

Padroes suportados (documentados em DECISOES.md):

  Sony XAVC S / AVCHD
    C0020.MP4  ->  C0020M01.XML          (mesma pasta)
    C0020.MP4  ->  C0020.XML             (variante)
    PRIVATE/M4ROOT/CLIP/C0020.MP4  ->  PRIVATE/M4ROOT/CLIP/C0020M01.XML

  DJI
    DJI_0001.MP4 -> DJI_0001.SRT / .LRF nao trazem data confiavel de relogio;
    quando existe DJI_0001.XML no padrao NRT (Non-Real-Time metadata), lemos.

  Generico
    <base>.xml com uma tag reconhecivel de data.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional
from xml.etree import ElementTree

from .mp4reader import parse_iso_datetime

# Tags/atributos que costumam carregar a data de criacao.
_DATE_ATTRS = ("value", "creationDate", "CreationDate", "createDate")
_DATE_TAGS = (
    "creationdate", "createdate", "creation_date", "captureddate",
    "modificationdate", "date",
)
_MAKE_TAGS = ("manufacturer", "make", "vendor")
_MODEL_TAGS = ("modelname", "model", "devicemodel")


@dataclass
class SidecarInfo:
    path: str
    time: Optional[datetime] = None
    make: str = ""
    model: str = ""
    duration: Optional[float] = None
    duration_frames: Optional[float] = None
    bytes_read: int = 0

    @property
    def useful(self) -> bool:
        return self.time is not None


def candidate_sidecars(media_path: str) -> List[str]:
    """Nomes de sidecar possiveis para um clipe, em ordem de preferencia."""
    folder = os.path.dirname(media_path)
    base = os.path.splitext(os.path.basename(media_path))[0]
    names = [f"{base}M01.XML", f"{base}M01.xml", f"{base}.XML", f"{base}.xml"]
    out = [os.path.join(folder, n) for n in names]
    # Sony tambem espelha os XML em .../CLIP ao lado de .../SUB
    parent = os.path.dirname(folder)
    if os.path.basename(folder).upper() == "SUB" and parent:
        clip = os.path.join(parent, "CLIP")
        out += [os.path.join(clip, n) for n in names]
    return out


def _local_name(tag: str) -> str:
    return tag.split("}")[-1].lower()


def read_sidecar(media_path: str, max_bytes: int = 512 * 1024) -> Optional[SidecarInfo]:
    """Procura e le o sidecar de um clipe. Retorna None se nao houver."""
    for cand in candidate_sidecars(media_path):
        if not os.path.isfile(cand):
            continue
        try:
            size = os.path.getsize(cand)
            if size > max_bytes:
                continue
            with open(cand, "rb") as fh:
                raw = fh.read(max_bytes)
        except OSError:
            continue
        info = SidecarInfo(path=cand, bytes_read=len(raw))
        _parse_xml_into(raw, info)
        if info.useful:
            return info
    return None


def _parse_xml_into(raw: bytes, info: SidecarInfo) -> None:
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        _parse_by_regex(raw, info)
        return

    for el in root.iter():
        name = _local_name(el.tag)
        attrs = {k.split("}")[-1]: v for k, v in el.attrib.items()}

        if info.time is None:
            # Sony: <CreationDate value="2026-08-13T10:03:00-03:00"/>
            if name in _DATE_TAGS or "date" in name:
                for key in _DATE_ATTRS:
                    if key in attrs:
                        dt = parse_iso_datetime(attrs[key])
                        if dt:
                            info.time = dt
                            break
                if info.time is None and el.text:
                    dt = parse_iso_datetime(el.text)
                    if dt:
                        info.time = dt
        if not info.make:
            if name in _MAKE_TAGS:
                info.make = (attrs.get("value") or el.text or "").strip()
            elif "manufacturer" in attrs:
                info.make = attrs["manufacturer"].strip()
        if not info.model:
            if name in _MODEL_TAGS:
                info.model = (attrs.get("value") or el.text or "").strip()
            elif "modelName" in el.attrib:
                info.model = el.attrib["modelName"].strip()
        # Sony: <Duration value="150"/>. O valor vem em FRAMES, nao em segundos,
        # e o sidecar nao diz o frame rate de forma confiavel — entao guardamos
        # so como dica e deixamos a duracao real vir dos atoms do video.
        if info.duration is None and name == "duration" and "value" in attrs:
            try:
                info.duration_frames = float(attrs["value"])
            except ValueError:
                pass


_RE_DATE = re.compile(rb'(?:creationDate|CreationDate|createDate)[^>]*?value="([^"]+)"')
_RE_MODEL = re.compile(rb'modelName="([^"]+)"')
_RE_MAKE = re.compile(rb'manufacturer="([^"]+)"')


def _parse_by_regex(raw: bytes, info: SidecarInfo) -> None:
    """Fallback para XML truncado/mal formado: procura os campos por regex."""
    m = _RE_DATE.search(raw)
    if m:
        info.time = parse_iso_datetime(m.group(1).decode("utf-8", "ignore"))
    m = _RE_MODEL.search(raw)
    if m:
        info.model = m.group(1).decode("utf-8", "ignore")
    m = _RE_MAKE.search(raw)
    if m:
        info.make = m.group(1).decode("utf-8", "ignore")


def write_sony_sidecar(media_path: str, when: datetime, model: str = "ILCE-7M3",
                       make: str = "Sony") -> str:
    """Escreve um sidecar no formato Sony — usado por `gerar_testes.py`."""
    folder = os.path.dirname(media_path)
    base = os.path.splitext(os.path.basename(media_path))[0]
    dest = os.path.join(folder, f"{base}M01.XML")
    stamp = when.strftime("%Y-%m-%dT%H:%M:%S")
    if when.utcoffset() is not None:
        total = int(when.utcoffset().total_seconds())
        sign = "+" if total >= 0 else "-"
        total = abs(total)
        stamp += f"{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<NonRealTimeMeta xmlns="urn:schemas-professionalDisc:nonRealTimeMeta">\n'
        f'  <TargetMaterial umidRef="{base}"/>\n'
        f'  <CreationDate value="{stamp}"/>\n'
        f'  <Device manufacturer="{make}" modelName="{model}"/>\n'
        '  <VideoFormat><VideoFrame formatFps="30p"/></VideoFormat>\n'
        '</NonRealTimeMeta>\n'
    )
    with open(dest, "w", encoding="utf-8") as fh:
        fh.write(xml)
    return dest
