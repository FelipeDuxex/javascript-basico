"""Configuracao do app e do projeto. Persistencia em JSON simples."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, List, Optional

APP_DIR = os.path.join(os.path.expanduser("~"), ".timeline_sync")
DEVICES_FILE = os.path.join(APP_DIR, "dispositivos.json")
CACHE_FILE = os.path.join(APP_DIR, "cache_metadados.sqlite")
PROJECTS_DIR = os.path.join(APP_DIR, "projetos")
MANIFEST_NAME = "manifesto.json"


def ensure_app_dir() -> str:
    os.makedirs(APP_DIR, exist_ok=True)
    os.makedirs(PROJECTS_DIR, exist_ok=True)
    return APP_DIR


@dataclass
class Config:
    """Parametros de organizacao. Tudo ajustavel pela interface."""

    # --- fuso / dia ----------------------------------------------------
    timezone: str = "America/Sao_Paulo"
    # Hora em que o "dia de gravacao" vira. 4h => gravacao ate 01:30 fica no
    # dia anterior, que e como a producao pensa.
    day_start_hour: float = 4.0

    # --- blocos --------------------------------------------------------
    block_method: str = "auto"          # auto | ratio | jenks | kmeans | fixo
    block_min_gap_seconds: float = 120.0     # piso: nunca quebra abaixo disso
    block_max_per_day: int = 6               # teto de blocos por dia
    block_min_clips_for_split: int = 6       # dia com menos clipes = 1 bloco
    block_min_ratio: float = 2.5             # salto minimo para considerar corte
    block_fixed_threshold_seconds: float = 1800.0   # usado com method="fixo"
    block_manual_threshold_all_days: Optional[float] = None

    # --- takes ---------------------------------------------------------
    take_window_seconds: float = 10.0

    # --- tempo morto ---------------------------------------------------
    gap_mode: str = "compress"          # preserve | compress | close
    gap_compress_factor: float = 0.1    # usado em compress
    gap_closed_seconds: float = 2.0     # usado em close
    gap_min_to_treat_seconds: float = 300.0  # vazios menores ficam intactos

    # --- exportacao ----------------------------------------------------
    sequence_timebase: int = 30
    sequence_width: int = 1080          # minisserie vertical
    sequence_height: int = 1920
    color_mode: str = "bloco"           # bloco | dia | dispositivo
    export_master: bool = True
    media_root_original: str = ""       # raiz detectada no material
    media_root_override: str = ""       # raiz para reescrever no XML

    # --- leitura -------------------------------------------------------
    workers: int = 6
    use_ffprobe_fallback: bool = True
    use_mtime_fallback: bool = True     # desligado automaticamente na nuvem
    max_bytes_per_file: int = 4 * 1024 * 1024
    recursive: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Config":
        known = {f.name for f in fields(cls)}
        clean = {k: v for k, v in (data or {}).items() if k in known}
        return cls(**clean)

    def update(self, data: Dict[str, Any]) -> "Config":
        known = {f.name: f for f in fields(self)}
        for key, value in (data or {}).items():
            if key not in known:
                continue
            current = getattr(self, key)
            try:
                if key == "block_manual_threshold_all_days":
                    setattr(self, key, None if value in (None, "", "auto") else float(value))
                elif isinstance(current, bool):
                    setattr(self, key, value if isinstance(value, bool)
                            else str(value).lower() in ("1", "true", "on", "yes"))
                elif isinstance(current, int) and not isinstance(current, bool):
                    setattr(self, key, int(float(value)))
                elif isinstance(current, float):
                    setattr(self, key, float(value))
                else:
                    setattr(self, key, value)
            except (TypeError, ValueError):
                continue
        return self


VALID_GAP_MODES = ("preserve", "compress", "close")
VALID_COLOR_MODES = ("bloco", "dia", "dispositivo")
VALID_BLOCK_METHODS = ("auto", "ratio", "jenks", "kmeans", "fixo")


@dataclass
class ProjectState:
    """Estado salvo por projeto: config + ajustes manuais de bloco.

    Guardar isso e o que permite reabrir o material da mesma producao semanas
    depois sem perder as decisoes tomadas na mao.
    """

    name: str = "projeto"
    folders: List[str] = field(default_factory=list)
    config: Config = field(default_factory=Config)
    # limiar manual por dia: {"2026-08-13": 2820.0}
    day_thresholds: Dict[str, float] = field(default_factory=dict)
    # fronteiras removidas/forcadas manualmente por dia, em ISO do instante
    forced_splits: Dict[str, List[str]] = field(default_factory=dict)
    merged_boundaries: Dict[str, List[str]] = field(default_factory=dict)
    device_aliases: Dict[str, str] = field(default_factory=dict)

    def path(self) -> str:
        ensure_app_dir()
        safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in self.name)
        return os.path.join(PROJECTS_DIR, f"{safe.strip() or 'projeto'}.json")

    def save(self) -> str:
        dest = self.path()
        data = {
            "name": self.name,
            "folders": self.folders,
            "config": self.config.to_dict(),
            "day_thresholds": self.day_thresholds,
            "forced_splits": self.forced_splits,
            "merged_boundaries": self.merged_boundaries,
            "device_aliases": self.device_aliases,
        }
        tmp = dest + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, dest)
        return dest

    @classmethod
    def load(cls, name: str) -> "ProjectState":
        state = cls(name=name)
        try:
            with open(state.path(), "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return state
        state.folders = data.get("folders") or []
        state.config = Config.from_dict(data.get("config") or {})
        state.day_thresholds = {k: float(v) for k, v in (data.get("day_thresholds") or {}).items()}
        state.forced_splits = data.get("forced_splits") or {}
        state.merged_boundaries = data.get("merged_boundaries") or {}
        state.device_aliases = data.get("device_aliases") or {}
        return state


def list_projects() -> List[str]:
    ensure_app_dir()
    out = []
    for name in sorted(os.listdir(PROJECTS_DIR)):
        if name.endswith(".json"):
            out.append(name[:-5])
    return out
