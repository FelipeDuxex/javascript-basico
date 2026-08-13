"""Relatorio final em texto (tambem exibido na tela)."""

from __future__ import annotations

import os
from typing import List, Optional, Sequence

from .config import Config
from .models import Project
from .timeutil import (format_bytes, format_duration, format_gap,
                       format_offset, hhmm)


def build_report(project: Project, config: Config,
                 xml_files: Optional[Sequence[str]] = None,
                 scan_seconds: float = 0.0) -> str:
    L: List[str] = []
    add = L.append

    add("=== RESUMO DA IMPORTACAO ===")
    add(
        f"{project.total_files} arquivos lidos | {len(project.devices)} dispositivos | "
        f"{len(project.days)} dias | {project.block_count} blocos | "
        f"{project.take_count} takes"
    )

    saved = ""
    if project.total_bytes:
        pct = project.bytes_read / project.total_bytes * 100
        saved = f" ({pct:.4f}% do material)"
    add(
        f"Leitura parcial: {format_bytes(project.bytes_read)} efetivamente "
        f"transferidos de {format_bytes(project.total_bytes)} totais{saved}"
    )
    if scan_seconds:
        add(f"Tempo de leitura: {format_duration(scan_seconds)} "
            f"({scan_seconds:.1f}s)")
    if project.read_methods:
        detalhe = ", ".join(f"{k}: {v}" for k, v in
                            sorted(project.read_methods.items(), key=lambda kv: -kv[1]))
        add(f"Estrategias de leitura: {detalhe}")
    if project.cloud_source:
        add("Origem detectada como volume de nuvem/rede — fallback de mtime desligado.")

    if project.fallback_clips:
        add("")
        add(f"{len(project.fallback_clips)} arquivo(s) sem creation_time "
            "(usaram fallback de data de modificacao) — listados abaixo:")
        for clip in project.fallback_clips:
            add(f"  ! {clip.filename} → {clip.start.strftime('%Y-%m-%d %H:%M:%S') if clip.start else '?'}"
                f" (mtime, confianca {clip.trust})")

    if project.unknown_time_clips:
        add("")
        add(f"{len(project.unknown_time_clips)} arquivo(s) SEM DATA UTILIZAVEL "
            "(ficaram fora da timeline):")
        for clip in project.unknown_time_clips:
            add(f"  x {clip.filename} — {'; '.join(clip.warnings) or 'sem creation_time'}")

    # --- dispositivos --------------------------------------------------
    add("")
    add("--- DISPOSITIVOS ---")
    for dev in project.devices:
        add(f"  {dev.label} [{dev.kind}] | {dev.clip_count} clipes | "
            f"offset {format_offset(dev.offset_seconds)} | {dev.clock_status}")
        for warn in dev.warnings:
            add(f"      ! {warn}")

    # --- estrutura -----------------------------------------------------
    add("")
    for day in project.days:
        add(f"DIA {day.index:02d} | {day.key} | {len(day.blocks)} blocos | "
            f"{len(day.clips)} clipes")
        for block in day.blocks:
            devices = " ".join(f"{k}({v})" for k, v in block.device_counts().items())
            add(f"  {block.label()} | {hhmm(block.start)}-{hhmm(block.end)} | "
                f"{format_duration(block.duration)} | {len(block.clips)} clipes | "
                f"{devices}")
        if day.threshold_seconds:
            add(f"  Limiar de pausa detectado ({day.threshold_source}): "
                f"{format_gap(day.threshold_seconds)}")
        else:
            add(f"  Bloco unico ({day.threshold_source})")
        if day.detection_note:
            add(f"  → {day.detection_note}")
        add("")

    # --- avisos --------------------------------------------------------
    if project.warnings:
        add("--- AVISOS ---")
        for warn in project.warnings:
            add(f"  ! {warn}")
        add("")

    orphans = project.orphan_takes()
    if orphans:
        add(f"--- TAKES ORFAOS ({len(orphans)}) — um unico dispositivo gravou ---")
        for take in orphans[:40]:
            add(f"  {take.start.strftime('%Y-%m-%d %H:%M:%S') if take.start else '?'} | "
                f"{', '.join(c.filename for c in take.clips)}")
        if len(orphans) > 40:
            add(f"  ... e outros {len(orphans) - 40}")
        add("")

    if project.duplicate_groups:
        add("--- DUPLICATAS ---")
        for group in project.duplicate_groups:
            add(f"  {group[0].filename}:")
            for clip in group:
                add(f"      {clip.path}")
        add("")

    if xml_files:
        add("--- XMLS GERADOS ---")
        for path in xml_files:
            add(f"  {os.path.abspath(path)}")
        add("")

    add("--- CONFIGURACAO USADA ---")
    add(f"  fuso: {config.timezone} | virada de dia: {config.day_start_hour:g}h")
    add(f"  blocos: metodo={config.block_method} piso={format_gap(config.block_min_gap_seconds)} "
        f"teto={config.block_max_per_day}/dia razao_min={config.block_min_ratio}x")
    add(f"  takes: janela de {config.take_window_seconds:g}s")
    add(f"  tempo morto: {config.gap_mode}"
        + (f" (fator {config.gap_compress_factor:g})" if config.gap_mode == "compress" else "")
        + (f" (para {config.gap_closed_seconds:g}s)" if config.gap_mode == "close" else ""))
    add(f"  cores: por {config.color_mode} | sequencia "
        f"{config.sequence_width}x{config.sequence_height}@{config.sequence_timebase}")
    return "\n".join(L) + "\n"


def write_report(project: Project, config: Config, dest: str,
                 xml_files: Optional[Sequence[str]] = None,
                 scan_seconds: float = 0.0) -> str:
    text = build_report(project, config, xml_files, scan_seconds)
    os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
    with open(dest, "w", encoding="utf-8") as fh:
        fh.write(text)
    return text
