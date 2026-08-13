"""Leitura de metadados: orquestracao das estrategias, do mais barato ao mais caro.

Ordem de tentativa por arquivo:

  1. manifesto.json na pasta  — zero I/O na midia (fluxo recomendado)
  2. cache local              — zero I/O na midia
  3. sidecar XML (Sony/DJI)   — poucos KB
  4. parser proprio de atoms  — dezenas de KB por leitura parcial
  5. ffprobe                  — caro no Drive, so como ultimo recurso
  6. mtime do arquivo         — desligado automaticamente na nuvem

Todo resultado das etapas 3-5 vai para o cache, entao um arquivo lido uma vez
nunca e lido de novo, mesmo em outra sessao do app.
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import ffprobe as ffprobe_mod
from . import mp4reader, sidecar, wavreader
from .cache import MetadataCache
from .config import Config
from .devices import (AUDIO_EXT, MEDIA_EXT, VIDEO_EXT, DeviceRegistry,
                      apply_clock_correction, default_label, identify,
                      track_index_for)
from .models import Clip

ATOM_EXT = {".mp4", ".mov", ".m4v", ".m4a", ".insv"}
WAV_EXT = {".wav", ".bwf", ".aif", ".aiff"}

ProgressCb = Callable[[Dict[str, Any]], None]


# ----------------------------------------------------------------------
# registro cru de metadado (o que vai para cache e manifesto)
# ----------------------------------------------------------------------

def _record_from_mp4(meta: mp4reader.Mp4Metadata, method: str) -> Dict[str, Any]:
    dt, source, tz_known = meta.best_time()
    video = meta.video_track
    audio = meta.audio_track
    duration = meta.duration
    if not duration:
        for track in meta.tracks:
            duration = max(duration, track.duration)
    return {
        "make": meta.tags.get("make", ""),
        "model": meta.tags.get("model", ""),
        "software": meta.tags.get("software", ""),
        "raw_time": dt.isoformat() if dt else None,
        "time_source": source,
        "tz_known": tz_known,
        "duration": duration,
        "width": video.width if video else 0,
        "height": video.height if video else 0,
        "codec": (video.codec if video else (audio.codec if audio else "")),
        "has_video": meta.has_video,
        "has_audio": meta.has_audio,
        "read_method": method,
        "bytes_read": meta.bytes_read,
        "error": meta.error,
    }


def _record_from_sidecar(info: sidecar.SidecarInfo, path: str) -> Dict[str, Any]:
    ext = os.path.splitext(path)[1].lower()
    return {
        "make": info.make,
        "model": info.model,
        "software": "",
        "raw_time": info.time.isoformat() if info.time else None,
        "time_source": "sidecar",
        "tz_known": info.time.tzinfo is not None if info.time else False,
        "duration": info.duration or 0.0,
        "width": 0, "height": 0, "codec": "",
        "has_video": ext in VIDEO_EXT,
        "has_audio": True,
        "read_method": "sidecar",
        "bytes_read": info.bytes_read,
        "error": "",
        "sidecar_path": info.path,
    }


def _empty_record(method: str, error: str = "") -> Dict[str, Any]:
    return {
        "make": "", "model": "", "software": "",
        "raw_time": None, "time_source": "none", "tz_known": False,
        "duration": 0.0, "width": 0, "height": 0, "codec": "",
        "has_video": False, "has_audio": False,
        "read_method": method, "bytes_read": 0, "error": error,
    }


def read_record(path: str, size: int, config: Optional[Config] = None,
                cache: Optional[MetadataCache] = None,
                manifest: Optional[Dict[str, Dict[str, Any]]] = None,
                allow_mtime: bool = True) -> Dict[str, Any]:
    """Le o registro cru de um arquivo, na ordem mais barata possivel."""
    config = config or Config()
    filename = os.path.basename(path)
    ext = os.path.splitext(filename)[1].lower()
    key = f"{filename.lower()}|{size}"

    if manifest and key in manifest:
        record = dict(manifest[key])
        record["read_method"] = "manifesto"
        record["bytes_read"] = 0
        return record

    if cache is not None:
        cached = cache.get(filename, size)
        if cached is not None:
            record = dict(cached)
            record["read_method"] = "cache"
            record["bytes_read"] = 0
            return record

    record: Optional[Dict[str, Any]] = None

    # 3. sidecar — o caminho mais barato quando existe
    info = sidecar.read_sidecar(path)
    if info is not None and info.useful:
        record = _record_from_sidecar(info, path)
        # Complementamos duracao/resolucao pelos atoms (ainda barato) porque o
        # sidecar Sony da duracao em frames, nao em segundos.
        if ext in ATOM_EXT:
            meta = mp4reader.read_metadata(path, max_bytes=config.max_bytes_per_file)
            if meta.ok:
                extra = _record_from_mp4(meta, "sidecar+atoms")
                record["duration"] = extra["duration"] or record["duration"]
                record["width"] = extra["width"]
                record["height"] = extra["height"]
                record["codec"] = extra["codec"]
                record["has_video"] = extra["has_video"]
                record["has_audio"] = extra["has_audio"]
                record["make"] = record["make"] or extra["make"]
                record["model"] = record["model"] or extra["model"]
                record["read_method"] = "sidecar+atoms"
                record["bytes_read"] += extra["bytes_read"]

    # 4a. WAV/BWF: data e hora vivem no chunk `bext`, no cabecalho
    if record is None and ext in WAV_EXT:
        wav = wavreader.read_metadata(path)
        if wav.ok and wav.time is not None:
            record = {
                "make": wav.tags.get("make", ""),
                "model": wav.tags.get("model", ""),
                "software": wav.tags.get("software", ""),
                "raw_time": wav.time.isoformat(),
                "time_source": wav.time_source,
                "tz_known": wav.time.tzinfo is not None,
                "duration": wav.duration,
                "width": 0, "height": 0,
                "codec": f"pcm_s{wav.bits}le" if wav.bits else "pcm",
                "has_video": False, "has_audio": True,
                "read_method": "bwf",
                "bytes_read": wav.bytes_read,
                "error": "",
            }

    # 4b. parser proprio de atoms
    if record is None and ext in ATOM_EXT:
        meta = mp4reader.read_metadata(path, max_bytes=config.max_bytes_per_file)
        if meta.ok and (meta.mvhd_time_literal or meta.tags):
            record = _record_from_mp4(meta, "atoms")

    # 5. ffprobe — caro na nuvem
    if record is None and config.use_ffprobe_fallback and ffprobe_mod.available():
        meta = ffprobe_mod.read_metadata(path)
        if meta.ok:
            record = _record_from_mp4(meta, "ffprobe")
            record["read_method"] = "ffprobe"

    if record is None:
        record = _empty_record("nenhum", "nenhuma estrategia conseguiu ler o arquivo")

    # 6. mtime — ultimo recurso, e apenas fora da nuvem
    if not record.get("raw_time"):
        if allow_mtime:
            try:
                mtime = os.path.getmtime(path)
                record["raw_time"] = datetime.fromtimestamp(mtime).isoformat()
                record["time_source"] = "mtime"
                record["tz_known"] = False
                record["read_method"] = record["read_method"] + "+mtime"
            except OSError:
                pass
        else:
            record["time_source"] = "indisponivel"

    if not record.get("has_video") and not record.get("has_audio"):
        record["has_video"] = ext in VIDEO_EXT
        record["has_audio"] = ext in AUDIO_EXT

    if cache is not None and record.get("raw_time"):
        storable = dict(record)
        storable.pop("read_method", None)
        cache.put(filename, size, storable)

    return record


def clip_from_record(path: str, size: int, record: Dict[str, Any]) -> Clip:
    """Monta o Clip (ainda sem correcao de relogio aplicada)."""
    filename = os.path.basename(path)
    ext = os.path.splitext(filename)[1].lower()
    raw_time = None
    if record.get("raw_time"):
        raw_time = mp4reader.parse_iso_datetime(record["raw_time"])

    has_video = bool(record.get("has_video"))
    has_audio = bool(record.get("has_audio"))
    make = record.get("make", "") or ""
    model = record.get("model", "") or ""
    kind, key = identify(filename, make, model, has_video, has_audio)

    clip = Clip(
        path=os.path.abspath(path),
        filename=filename,
        size=size,
        ext=ext,
        device_key=key,
        device_kind=kind,
        device_label=default_label(kind, model, key),
        make=make,
        model=model,
        raw_time=raw_time,
        time_source=record.get("time_source", "none"),
        tz_known=bool(record.get("tz_known")),
        duration=float(record.get("duration") or 0.0),
        width=int(record.get("width") or 0),
        height=int(record.get("height") or 0),
        codec=record.get("codec", "") or "",
        has_video=has_video,
        has_audio=has_audio,
        read_method=record.get("read_method", "") or "",
        bytes_read=int(record.get("bytes_read") or 0),
    )
    if record.get("time_source") == "mtime":
        clip.fallback = True
        clip.warnings.append(
            "sem creation_time no arquivo — usou a data de modificacao (menos confiavel)"
        )
    if record.get("time_source") == "indisponivel":
        clip.warnings.append(
            "sem creation_time e origem na nuvem — data desconhecida, resolva manualmente"
        )
    if record.get("error"):
        clip.warnings.append(str(record["error"]))
    if clip.duration <= 0:
        clip.duration = 5.0
        clip.warnings.append("duracao nao lida — assumidos 5s para posicionamento")
    return clip


def read_one(path: str, use_ffprobe: bool = True, use_mtime: bool = True,
             config: Optional[Config] = None) -> Clip:
    """Le um arquivo isolado (usado pela calibracao). Sem cache."""
    cfg = config or Config()
    cfg.use_ffprobe_fallback = use_ffprobe
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    record = read_record(path, size, config=cfg, cache=None, manifest=None,
                         allow_mtime=use_mtime)
    return clip_from_record(path, size, record)


# ----------------------------------------------------------------------
# varredura de pastas
# ----------------------------------------------------------------------

def find_media(folders: List[str], recursive: bool = True) -> List[Tuple[str, int]]:
    """Lista arquivos de midia (caminho, tamanho), sem duplicar caminhos."""
    seen: Dict[str, int] = {}
    for folder in folders:
        folder = os.path.expanduser(folder)
        if os.path.isfile(folder):
            ext = os.path.splitext(folder)[1].lower()
            if ext in MEDIA_EXT:
                seen[os.path.abspath(folder)] = _size(folder)
            continue
        if not os.path.isdir(folder):
            continue
        if recursive:
            for root, dirs, files in os.walk(folder):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for name in files:
                    if os.path.splitext(name)[1].lower() in MEDIA_EXT:
                        full = os.path.abspath(os.path.join(root, name))
                        seen[full] = _size(full)
        else:
            for name in sorted(os.listdir(folder)):
                full = os.path.abspath(os.path.join(folder, name))
                if os.path.isfile(full) and os.path.splitext(name)[1].lower() in MEDIA_EXT:
                    seen[full] = _size(full)
    return sorted(seen.items())


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


@dataclass
class ScanResult:
    clips: List[Clip] = field(default_factory=list)
    total_files: int = 0
    total_bytes: int = 0
    bytes_read: int = 0
    seconds: float = 0.0
    cancelled: bool = False
    read_methods: Dict[str, int] = field(default_factory=dict)
    cache_hits: int = 0
    manifest_hits: int = 0
    warnings: List[str] = field(default_factory=list)


def scan(folders: List[str], config: Config, registry: DeviceRegistry,
         cache: Optional[MetadataCache] = None,
         progress: Optional[ProgressCb] = None,
         cancel: Optional[threading.Event] = None,
         allow_mtime: Optional[bool] = None) -> ScanResult:
    """Le todas as midias das pastas, em paralelo, com progresso e cancelamento.

    Cancelar nao perde trabalho: o que ja foi lido esta no cache.
    """
    from .manifest import load_manifests

    started = time.time()
    result = ScanResult()
    files = find_media(folders, recursive=config.recursive)
    result.total_files = len(files)
    result.total_bytes = sum(size for _, size in files)

    manifest = load_manifests(folders)
    if manifest:
        result.warnings.append(
            f"manifesto de ingestao encontrado: {len(manifest)} arquivos lidos sem "
            "tocar na midia"
        )

    if allow_mtime is None:
        allow_mtime = config.use_mtime_fallback

    def worker(item: Tuple[str, int]) -> Optional[Clip]:
        path, size = item
        if cancel is not None and cancel.is_set():
            return None
        record = read_record(path, size, config=config, cache=cache,
                             manifest=manifest, allow_mtime=allow_mtime)
        return clip_from_record(path, size, record)

    done = 0
    workers = max(1, min(int(config.workers or 4), 32))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(worker, item): item for item in files}
        for future in as_completed(futures):
            path, _size_ = futures[future]
            done += 1
            try:
                clip = future.result()
            except Exception as exc:  # nunca deixa um arquivo ruim derrubar o lote
                clip = None
                result.warnings.append(f"{os.path.basename(path)}: {exc}")
            if clip is not None:
                result.clips.append(clip)
                result.bytes_read += clip.bytes_read
                method = clip.read_method or "?"
                result.read_methods[method] = result.read_methods.get(method, 0) + 1
                if method == "cache":
                    result.cache_hits += 1
                elif method == "manifesto":
                    result.manifest_hits += 1
            if progress is not None:
                elapsed = time.time() - started
                rate = done / elapsed if elapsed > 0 else 0
                remaining = (len(files) - done) / rate if rate > 0 else 0
                progress({
                    "done": done,
                    "total": len(files),
                    "current": os.path.basename(path),
                    "elapsed": elapsed,
                    "eta": remaining,
                    "bytes_read": result.bytes_read,
                })
            if cancel is not None and cancel.is_set():
                result.cancelled = True
                break

    # aplica identificacao/alias/offset por dispositivo
    assigned: Dict[str, int] = {}
    for clip in sorted(result.clips, key=lambda c: (c.device_kind, c.device_key)):
        profile = registry.ensure(clip.device_key, clip.device_kind,
                                  clip.make, clip.model)
        clip.device_label = profile.label or clip.device_label
        clip.track_index = track_index_for(clip.device_kind, clip.device_key, assigned)
        apply_clock_correction(clip, profile, config.timezone)
    registry.save()

    result.seconds = time.time() - started
    return result
