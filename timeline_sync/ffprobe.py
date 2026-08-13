"""Fallback via `ffprobe`.

Usado somente quando o parser proprio de atoms nao conseguiu interpretar o
arquivo. No Google Drive isso e caro: o `ffprobe` pode arrastar bem mais bytes
que os poucos KB da leitura parcial. Por isso todo uso e registrado com aviso.
"""

from __future__ import annotations

import json
import os
import subprocess

from .mp4reader import Mp4Metadata, TrackInfo, parse_iso_datetime
from .runtime import find_tool


def _binario() -> str:
    # Resolvido a cada chamada: no executavel congelado o usuario pode largar o
    # ffprobe.exe ao lado do app depois que ele ja estava aberto.
    return find_tool("ffprobe") or "ffprobe"


def available() -> bool:
    return find_tool("ffprobe") is not None


def read_metadata(path: str, timeout: float = 120.0) -> Mp4Metadata:
    """Le metadados com `ffprobe` e devolve no mesmo formato do parser proprio."""
    meta = Mp4Metadata(path=path)
    try:
        meta.size = os.path.getsize(path)
    except OSError:
        pass
    cmd = [
        _binario(), "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        meta.error = f"ffprobe falhou: {exc}"
        return meta
    if proc.returncode != 0:
        meta.error = f"ffprobe rc={proc.returncode}: {proc.stderr.decode(errors='ignore')[:200]}"
        return meta
    try:
        data = json.loads(proc.stdout.decode("utf-8", "ignore"))
    except json.JSONDecodeError as exc:
        meta.error = f"json invalido do ffprobe: {exc}"
        return meta

    fmt = data.get("format", {})
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    meta.major_brand = (tags.get("major_brand") or "").strip()
    try:
        meta.duration = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        meta.duration = 0.0

    created = tags.get("creation_time")
    dt = parse_iso_datetime(created) if created else None
    if dt is not None:
        # ffprobe entrega mvhd normalizado como UTC. Guardamos os dois usos:
        # o instante "aware" e o valor literal do relogio.
        meta.mvhd_time_utc = dt
        meta.mvhd_time_literal = dt.replace(tzinfo=None)

    for key, logical in (
        ("com.apple.quicktime.model", "model"),
        ("com.apple.quicktime.make", "make"),
        ("com.apple.quicktime.software", "software"),
        ("com.apple.quicktime.creationdate", "apple_creationdate"),
        ("model", "model"),
        ("make", "make"),
        ("encoder", "encoder"),
        ("date", "date"),
    ):
        value = tags.get(key)
        if value:
            meta.tags.setdefault(logical, value)

    for stream in data.get("streams", []):
        kind = {"video": "vide", "audio": "soun", "data": "data"}.get(
            stream.get("codec_type", ""), stream.get("codec_type", "unknown")
        )
        track = TrackInfo(
            kind=kind,
            codec=stream.get("codec_name", ""),
            width=int(stream.get("width") or 0),
            height=int(stream.get("height") or 0),
            sample_rate=int(stream.get("sample_rate") or 0),
            channels=int(stream.get("channels") or 0),
        )
        try:
            track.duration = float(stream.get("duration") or 0.0)
        except (TypeError, ValueError):
            pass
        stags = {k.lower(): v for k, v in (stream.get("tags") or {}).items()}
        if not meta.tags.get("model") and stags.get("model"):
            meta.tags["model"] = stags["model"]
        meta.tracks.append(track)

    meta.ok = meta.mvhd_time_literal is not None or bool(meta.tracks)
    # Estimativa conservadora: o ffprobe le cabecalho + indices. Nao temos como
    # medir exatamente, entao registramos 0 e sinalizamos via read_method.
    meta.bytes_read = 0
    return meta
