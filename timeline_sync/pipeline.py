"""Orquestracao: varredura -> organizacao -> exportacao -> relatorio.

Usado pela CLI e pela interface web, para as duas terem exatamente o mesmo
comportamento. A separacao importante e:

  `Session.scan()`   — caro (le a midia). Roda uma vez.
  `Session.rebuild()` — barato (so reagrupa os clipes ja lidos). Roda a cada
                        ajuste de parametro, o que permite mexer no slider de
                        limiar e ver a timeline se reorganizar na hora.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from . import cloud, fcpxml, report
from .cache import MetadataCache
from .config import Config, ProjectState
from .devices import DeviceRegistry
from .grouping import build_project
from .models import Clip, Project
from .reader import ScanResult, scan
from .runtime import tool_status
from .timeline import build_layout


@dataclass
class Progress:
    running: bool = False
    done: int = 0
    total: int = 0
    current: str = ""
    elapsed: float = 0.0
    eta: float = 0.0
    bytes_read: int = 0
    message: str = ""
    cancelled: bool = False
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "running": self.running, "done": self.done, "total": self.total,
            "current": self.current, "elapsed": self.elapsed, "eta": self.eta,
            "bytes_read": self.bytes_read, "message": self.message,
            "cancelled": self.cancelled, "error": self.error,
            "pct": (self.done / self.total * 100) if self.total else 0.0,
        }


class Session:
    """Estado vivo de trabalho sobre um conjunto de pastas."""

    def __init__(self, state: Optional[ProjectState] = None,
                 registry: Optional[DeviceRegistry] = None,
                 cache: Optional[MetadataCache] = None):
        self.state = state or ProjectState()
        self.registry = registry or DeviceRegistry()
        self.cache = cache if cache is not None else MetadataCache()
        self.clips: List[Clip] = []
        self.project: Optional[Project] = None
        self.scan_result: Optional[ScanResult] = None
        self.progress = Progress()
        self.volume: Optional[cloud.VolumeInfo] = None
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    # -- config ---------------------------------------------------------
    @property
    def config(self) -> Config:
        return self.state.config

    def update_config(self, data: Dict[str, Any]) -> Project:
        self.config.update(data)
        self.state.save()
        return self.rebuild()

    # -- varredura ------------------------------------------------------
    def scan_sync(self, folders: Sequence[str]) -> Project:
        """Varredura bloqueante (usada pela CLI)."""
        self._run_scan(list(folders))
        if self.progress.error:
            raise RuntimeError(self.progress.error)
        assert self.project is not None
        return self.project

    def scan_async(self, folders: Sequence[str]) -> bool:
        """Varredura em thread (usada pela interface web)."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._cancel.clear()
            self._thread = threading.Thread(
                target=self._run_scan, args=(list(folders),), daemon=True
            )
            self._thread.start()
        return True

    def cancel(self) -> None:
        """Cancela sem perder trabalho: o que ja foi lido esta no cache."""
        self._cancel.set()

    def is_scanning(self) -> bool:
        """True enquanto a varredura em thread nao terminou."""
        if self.progress.running:
            return True
        thread = self._thread
        return thread is not None and thread.is_alive()

    def _run_scan(self, folders: List[str]) -> None:
        self.progress = Progress(running=True, message="preparando...")
        try:
            folders = [os.path.expanduser(f) for f in folders if f.strip()]
            missing = [f for f in folders if not os.path.exists(f)]
            if missing:
                raise FileNotFoundError("pasta nao encontrada: " + ", ".join(missing))
            self.state.folders = folders

            self.volume = cloud.inspect_many(folders)
            allow_mtime = self.config.use_mtime_fallback
            if self.volume.is_cloud:
                # Na nuvem o mtime reflete a sincronizacao, nao a gravacao.
                allow_mtime = False
                self.progress.message = self.volume.warning or ""

            if not self.config.media_root_original and folders:
                self.config.media_root_original = os.path.commonpath(
                    [os.path.abspath(f) for f in folders]
                ) if len(folders) > 1 else os.path.abspath(folders[0])

            def on_progress(info: Dict[str, Any]) -> None:
                self.progress.done = info["done"]
                self.progress.total = info["total"]
                self.progress.current = info["current"]
                self.progress.elapsed = info["elapsed"]
                self.progress.eta = info["eta"]
                self.progress.bytes_read = info["bytes_read"]

            result = scan(folders, self.config, self.registry, cache=self.cache,
                          progress=on_progress, cancel=self._cancel,
                          allow_mtime=allow_mtime)
            self.scan_result = result
            self.clips = result.clips
            self.progress.cancelled = result.cancelled
            self.rebuild()
            self.state.save()
            msgs = list(result.warnings)
            if self.volume and self.volume.warning:
                msgs.insert(0, self.volume.warning)
            self.progress.message = " | ".join(msgs)
        except Exception as exc:
            self.progress.error = f"{type(exc).__name__}: {exc}"
        finally:
            self.progress.running = False

    # -- organizacao ----------------------------------------------------
    def rebuild(self) -> Project:
        """Reagrupa a partir dos clipes ja lidos. Barato — roda a cada ajuste."""
        # Reaplica alias e offset atuais dos perfis antes de reagrupar, para o
        # slider e o cadastro de dispositivo terem efeito imediato.
        from .devices import apply_clock_correction
        for clip in self.clips:
            profile = self.registry.get(clip.device_key)
            if profile is not None:
                clip.device_label = profile.label or clip.device_label
            clip.warnings = [w for w in clip.warnings
                             if "sem calibracao" not in w]
            apply_clock_correction(clip, profile, self.config.timezone)

        project = build_project(self.clips, self.config, self.registry, self.state)
        if self.scan_result is not None:
            project.total_files = self.scan_result.total_files
            project.total_bytes = self.scan_result.total_bytes
            project.bytes_read = self.scan_result.bytes_read
            project.read_seconds = self.scan_result.seconds
            project.read_methods = dict(self.scan_result.read_methods)
        if self.volume is not None:
            project.cloud_source = self.volume.is_cloud
            if self.volume.warning:
                project.warnings.insert(0, self.volume.warning)
        self.project = project
        return project

    # -- ajustes manuais de bloco ---------------------------------------
    def set_day_threshold(self, day_key: str, seconds: Optional[float]) -> Project:
        if seconds is None:
            self.state.day_thresholds.pop(day_key, None)
        else:
            self.state.day_thresholds[day_key] = float(seconds)
        self.state.save()
        return self.rebuild()

    def set_global_threshold(self, seconds: Optional[float]) -> Project:
        self.config.block_manual_threshold_all_days = (
            None if seconds is None else float(seconds)
        )
        # Um limiar global substitui os ajustes por dia, senao o resultado
        # dependeria de qual foi mexido por ultimo.
        if seconds is not None:
            self.state.day_thresholds.clear()
        self.state.save()
        return self.rebuild()

    def merge_block(self, day_key: str, block_index: int) -> Project:
        """Mescla o bloco indicado com o anterior."""
        day = self._day(day_key)
        if day is None or block_index <= 1 or block_index > len(day.blocks):
            return self.project or self.rebuild()
        block = day.blocks[block_index - 1]
        if block.start is None:
            return self.project or self.rebuild()
        iso = block.start.isoformat()
        removed = self.state.merged_boundaries.setdefault(day_key, [])
        if iso not in removed:
            removed.append(iso)
        forced = self.state.forced_splits.get(day_key, [])
        if iso in forced:
            forced.remove(iso)
        self.state.save()
        return self.rebuild()

    def split_block(self, day_key: str, at_iso: str) -> Project:
        """Forca uma divisao de bloco no instante indicado (inicio de um clipe)."""
        forced = self.state.forced_splits.setdefault(day_key, [])
        if at_iso not in forced:
            forced.append(at_iso)
        removed = self.state.merged_boundaries.get(day_key, [])
        if at_iso in removed:
            removed.remove(at_iso)
        self.state.save()
        return self.rebuild()

    def reset_day(self, day_key: str) -> Project:
        self.state.day_thresholds.pop(day_key, None)
        self.state.forced_splits.pop(day_key, None)
        self.state.merged_boundaries.pop(day_key, None)
        self.state.save()
        return self.rebuild()

    def _day(self, day_key: str):
        if self.project is None:
            return None
        return next((d for d in self.project.days if d.key == day_key), None)

    # -- exportacao -----------------------------------------------------
    def export(self, out_dir: str, day_keys: Optional[Sequence[str]] = None,
               include_master: Optional[bool] = None,
               one_file_per_day: bool = True) -> Dict[str, Any]:
        if self.project is None:
            self.rebuild()
        assert self.project is not None
        result = fcpxml.export(
            self.project, self.config, out_dir, day_keys=day_keys,
            include_master=include_master, one_file_per_day=one_file_per_day,
            project_name=self.state.name,
        )
        report_path = os.path.join(out_dir, "relatorio.txt")
        text = report.write_report(self.project, self.config, report_path,
                                  xml_files=result.files,
                                  scan_seconds=self.project.read_seconds)
        return {
            "files": result.files,
            "sequences": result.sequences,
            "warnings": result.warnings,
            "clip_count": result.clip_count,
            "report_path": report_path,
            "report": text,
        }

    # -- serializacao para a interface ----------------------------------
    def snapshot(self) -> Dict[str, Any]:
        project = self.project
        data: Dict[str, Any] = {
            "config": self.config.to_dict(),
            "state": {
                "name": self.state.name,
                "folders": self.state.folders,
                "day_thresholds": self.state.day_thresholds,
                "forced_splits": self.state.forced_splits,
                "merged_boundaries": self.state.merged_boundaries,
            },
            "progress": self.progress.to_dict(),
            "profiles": [p.to_dict() | {"display": p.display(),
                                        "total_offset": p.total_offset}
                         for p in self.registry.profiles.values()],
            "cache_count": self.cache.count(),
            "ferramentas": tool_status(),
            "volume": {
                "is_cloud": bool(self.volume and self.volume.is_cloud),
                "provider": self.volume.provider if self.volume else "",
                "warning": (self.volume.warning if self.volume else None) or "",
            },
        }
        if project is None:
            data["project"] = None
            return data

        layout = build_layout(project.clips)
        data["project"] = project.to_dict()
        data["layout"] = {
            "video_count": layout.video_count,
            "audio_count": layout.audio_count,
            "slots": [
                {
                    "device_key": s.device_key, "label": s.label, "kind": s.kind,
                    "video_index": s.video_index, "audio_index": s.audio_index,
                    "order": s.order,
                }
                for s in sorted(layout.slots.values(), key=lambda s: s.order)
            ],
        }
        data["timelines"] = [self.timeline_payload(day.key) for day in project.days]
        data["report"] = report.build_report(project, self.config,
                                            scan_seconds=project.read_seconds)
        return data

    def timeline_payload(self, day_key: str) -> Dict[str, Any]:
        """Dados de desenho de um dia: mesma logica de cor da exportacao."""
        from .timeline import build_day_sequence
        day = self._day(day_key)
        if day is None:
            return {"day": day_key, "clips": [], "markers": [], "duration": 0}
        layout = build_layout(self.project.clips if self.project else day.clips)
        seq = build_day_sequence(day, self.config, layout)
        from .colors import hex_for
        return {
            "day": day.key,
            "label": day.label(),
            "sequence_name": seq.name,
            "duration": seq.duration,
            "removed_seconds": seq.removed_seconds,
            "threshold_seconds": day.threshold_seconds,
            "threshold_source": day.threshold_source,
            "detection_note": day.detection_note,
            "blocks": [b.to_dict() for b in day.blocks],
            "markers": [
                {"name": m.name, "comment": m.comment, "at": m.at,
                 "color": m.color, "hex": hex_for(m.color), "kind": m.kind}
                for m in seq.markers
            ],
            "clips": [
                {
                    "filename": p.clip.filename,
                    "path": p.clip.path,
                    "device_key": p.clip.device_key,
                    "device_label": p.clip.device_label,
                    "video_index": p.slot.video_index,
                    "audio_index": p.slot.audio_index,
                    "start": p.timeline_start,
                    "duration": p.duration,
                    "color": p.label_color,
                    "hex": hex_for(p.label_color),
                    "block_index": p.block_index,
                    "take_index": p.clip.take_index,
                    "real_start": p.clip.start.isoformat() if p.clip.start else None,
                    "raw_time": p.clip.raw_time.isoformat() if p.clip.raw_time else None,
                    "time_source": p.clip.time_source,
                    "offset_seconds": p.clip.offset_seconds,
                    "trust": p.clip.trust,
                    "fallback": p.clip.fallback,
                    "read_method": p.clip.read_method,
                    "codec": p.clip.codec,
                    "width": p.clip.width,
                    "height": p.clip.height,
                    "size": p.clip.size,
                    "has_audio": p.clip.has_audio,
                    "has_video": p.clip.has_video,
                    "warnings": p.clip.warnings,
                }
                for p in seq.clips
            ],
        }

    def close(self) -> None:
        self.cache.close()
