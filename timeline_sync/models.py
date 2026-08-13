"""Modelo de dados: CLIPE -> TAKE -> BLOCO -> DIA."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as _date
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

# Confianca do horario lido, do melhor para o pior.
TRUST_HIGH = "alta"        # iPhone com fuso explicito, ou sidecar com fuso
TRUST_MEDIUM = "media"     # relogio literal + offset calibrado
TRUST_LOW = "baixa"        # relogio literal sem calibracao, ou mtime local
TRUST_NONE = "desconhecida"  # sem data (ex.: nuvem sem creation_time)

DEVICE_KINDS = ("sony", "iphone", "dji", "hollyland", "desconhecido")


@dataclass
class Clip:
    """Um arquivo individual de video ou audio."""

    path: str
    filename: str
    size: int = 0
    ext: str = ""

    device_key: str = "desconhecido"    # chave canonica do perfil
    device_kind: str = "desconhecido"
    device_label: str = "Desconhecido"
    make: str = ""
    model: str = ""

    raw_time: Optional[datetime] = None     # como lido (aware ou naive)
    time_source: str = "none"               # sidecar | quicktime.creationdate | mvhd | ...
    tz_known: bool = False
    offset_seconds: float = 0.0             # correcao aplicada
    start: Optional[datetime] = None        # horario corrigido, naive local
    duration: float = 0.0

    width: int = 0
    height: int = 0
    codec: str = ""
    has_video: bool = False
    has_audio: bool = False

    read_method: str = ""       # manifesto | cache | sidecar | atoms | ffprobe | mtime
    bytes_read: int = 0
    trust: str = TRUST_NONE
    fallback: bool = False      # usou mtime ou algo menos confiavel
    warnings: List[str] = field(default_factory=list)

    # preenchidos pelo agrupamento
    day_key: str = ""
    block_index: int = 0
    take_index: int = 0
    track_index: int = 0

    @property
    def end(self) -> Optional[datetime]:
        if self.start is None:
            return None
        return self.start + timedelta(seconds=max(self.duration, 0.0))

    @property
    def is_audio_only(self) -> bool:
        return self.has_audio and not self.has_video

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "filename": self.filename,
            "size": self.size,
            "device_key": self.device_key,
            "device_kind": self.device_kind,
            "device_label": self.device_label,
            "make": self.make,
            "model": self.model,
            "raw_time": self.raw_time.isoformat() if self.raw_time else None,
            "time_source": self.time_source,
            "tz_known": self.tz_known,
            "offset_seconds": self.offset_seconds,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "duration": self.duration,
            "width": self.width,
            "height": self.height,
            "codec": self.codec,
            "has_video": self.has_video,
            "has_audio": self.has_audio,
            "read_method": self.read_method,
            "bytes_read": self.bytes_read,
            "trust": self.trust,
            "fallback": self.fallback,
            "warnings": self.warnings,
            "day_key": self.day_key,
            "block_index": self.block_index,
            "take_index": self.take_index,
        }


@dataclass
class Take:
    """Clipes de dispositivos diferentes gravados no mesmo momento."""

    index: int
    clips: List[Clip] = field(default_factory=list)

    @property
    def start(self) -> Optional[datetime]:
        times = [c.start for c in self.clips if c.start]
        return min(times) if times else None

    @property
    def end(self) -> Optional[datetime]:
        times = [c.end for c in self.clips if c.end]
        return max(times) if times else None

    @property
    def devices(self) -> List[str]:
        seen: List[str] = []
        for c in self.clips:
            if c.device_key not in seen:
                seen.append(c.device_key)
        return seen

    @property
    def orphan(self) -> bool:
        """Take coberto por um unico dispositivo — geralmente erro de set."""
        return len(self.devices) == 1

    @property
    def has_audio(self) -> bool:
        return any(c.has_audio for c in self.clips)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "devices": self.devices,
            "orphan": self.orphan,
            "has_audio": self.has_audio,
            "clips": [c.filename for c in self.clips],
        }


@dataclass
class Block:
    """Janela continua de gravacao dentro de um dia."""

    index: int
    takes: List[Take] = field(default_factory=list)

    @property
    def clips(self) -> List[Clip]:
        return [c for t in self.takes for c in t.clips]

    @property
    def start(self) -> Optional[datetime]:
        times = [t.start for t in self.takes if t.start]
        return min(times) if times else None

    @property
    def end(self) -> Optional[datetime]:
        times = [t.end for t in self.takes if t.end]
        return max(times) if times else None

    @property
    def duration(self) -> float:
        if self.start and self.end:
            return (self.end - self.start).total_seconds()
        return 0.0

    def device_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for c in self.clips:
            counts[c.device_label] = counts.get(c.device_label, 0) + 1
        return counts

    def label(self) -> str:
        return f"BLOCO {self.index:02d}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "label": self.label(),
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "duration": self.duration,
            "clip_count": len(self.clips),
            "take_count": len(self.takes),
            "device_counts": self.device_counts(),
            "takes": [t.to_dict() for t in self.takes],
        }


@dataclass
class Day:
    """Uma data-calendario de gravacao (apos a correcao de horario)."""

    index: int
    key: str                      # "2026-08-13"
    blocks: List[Block] = field(default_factory=list)
    threshold_seconds: Optional[float] = None
    threshold_source: str = "auto"     # auto | manual | global | fixo | unico
    detection_note: str = ""

    @property
    def clips(self) -> List[Clip]:
        return [c for b in self.blocks for c in b.clips]

    @property
    def date(self) -> Optional[_date]:
        try:
            return datetime.strptime(self.key, "%Y-%m-%d").date()
        except ValueError:
            return None

    @property
    def start(self) -> Optional[datetime]:
        times = [b.start for b in self.blocks if b.start]
        return min(times) if times else None

    @property
    def end(self) -> Optional[datetime]:
        times = [b.end for b in self.blocks if b.end]
        return max(times) if times else None

    def label(self) -> str:
        return f"DIA {self.index:02d}"

    def sequence_name(self) -> str:
        return f"{self.label()} — {self.key}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "key": self.key,
            "label": self.label(),
            "sequence_name": self.sequence_name(),
            "threshold_seconds": self.threshold_seconds,
            "threshold_source": self.threshold_source,
            "detection_note": self.detection_note,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "clip_count": len(self.clips),
            "blocks": [b.to_dict() for b in self.blocks],
        }


@dataclass
class DeviceSummary:
    key: str
    kind: str
    label: str
    make: str = ""
    model: str = ""
    clip_count: int = 0
    offset_seconds: float = 0.0
    calibrated: bool = False
    calibrated_at: str = ""
    track_index: int = 0
    # Dispositivo que grava o fuso explicitamente (iPhone): nao precisa de
    # calibracao, e serve de referencia para calibrar os outros.
    is_reference: bool = False
    warnings: List[str] = field(default_factory=list)

    @property
    def clock_status(self) -> str:
        if self.is_reference:
            return "referencia de hora (fuso explicito no metadado)"
        if self.calibrated:
            return "calibrado " + (self.calibrated_at[:10] or "")
        return "NAO CALIBRADO"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key, "kind": self.kind, "label": self.label,
            "make": self.make, "model": self.model,
            "clip_count": self.clip_count,
            "offset_seconds": self.offset_seconds,
            "calibrated": self.calibrated,
            "calibrated_at": self.calibrated_at,
            "track_index": self.track_index,
            "is_reference": self.is_reference,
            "clock_status": self.clock_status,
            "warnings": self.warnings,
        }


@dataclass
class Project:
    """Resultado completo de uma organizacao."""

    days: List[Day] = field(default_factory=list)
    devices: List[DeviceSummary] = field(default_factory=list)
    unknown_time_clips: List[Clip] = field(default_factory=list)
    fallback_clips: List[Clip] = field(default_factory=list)
    duplicate_groups: List[List[Clip]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    total_files: int = 0
    total_bytes: int = 0
    bytes_read: int = 0
    read_seconds: float = 0.0
    cloud_source: bool = False
    read_methods: Dict[str, int] = field(default_factory=dict)

    @property
    def clips(self) -> List[Clip]:
        return [c for d in self.days for c in d.clips]

    @property
    def block_count(self) -> int:
        return sum(len(d.blocks) for d in self.days)

    @property
    def take_count(self) -> int:
        return sum(len(b.takes) for d in self.days for b in d.blocks)

    def orphan_takes(self) -> List[Take]:
        return [t for d in self.days for b in d.blocks for t in b.takes if t.orphan]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "days": [d.to_dict() for d in self.days],
            "devices": [d.to_dict() for d in self.devices],
            "unknown_time": [c.to_dict() for c in self.unknown_time_clips],
            "fallback": [c.to_dict() for c in self.fallback_clips],
            "duplicates": [[c.path for c in g] for g in self.duplicate_groups],
            "warnings": self.warnings,
            "total_files": self.total_files,
            "total_bytes": self.total_bytes,
            "bytes_read": self.bytes_read,
            "read_seconds": self.read_seconds,
            "cloud_source": self.cloud_source,
            "read_methods": self.read_methods,
            "block_count": self.block_count,
            "take_count": self.take_count,
            "orphan_takes": len(self.orphan_takes()),
        }
