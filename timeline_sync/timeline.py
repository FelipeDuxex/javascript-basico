"""Montagem da timeline: trilhas por dispositivo e fechamento de tempo morto.

Duas responsabilidades:

1. **Layout de trilhas** — cada dispositivo em sua propria trilha, sempre na
   mesma posicao vertical (Sony V1, iPhone V2, DJI V3, Hollyland na trilha de
   audio). A posicao identifica o aparelho; a cor identifica o bloco.

2. **Tempo morto** — uma diaria com 3h de intervalo geraria uma timeline com 3h
   de vazio. `TimeMap` mapeia hora real -> hora na timeline, podendo preservar,
   comprimir ou fechar os vazios.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from .colors import label_for
from .config import Config
from .grouping import consolidated_gaps
from .models import Clip, Day, Project

# Separacao visual minima entre dias na sequencia MASTER.
MASTER_DAY_GAP = 5.0


# ----------------------------------------------------------------------
# layout de trilhas
# ----------------------------------------------------------------------

@dataclass
class TrackSlot:
    device_key: str
    label: str
    kind: str
    video_index: int = 0     # 1-based; 0 = nao tem trilha de video
    audio_index: int = 0     # 1-based; 0 = nao tem trilha de audio
    order: int = 0           # ordem estavel para escolher cor no modo dispositivo


@dataclass
class TrackLayout:
    slots: Dict[str, TrackSlot] = field(default_factory=dict)
    video_count: int = 0
    audio_count: int = 0

    def get(self, device_key: str) -> Optional[TrackSlot]:
        return self.slots.get(device_key)

    def describe(self) -> List[str]:
        out = []
        for slot in sorted(self.slots.values(), key=lambda s: s.order):
            where = []
            if slot.video_index:
                where.append(f"V{slot.video_index}")
            if slot.audio_index:
                where.append(f"A{slot.audio_index}")
            out.append(f"{slot.label}: {'+'.join(where) or '-'}")
        return out


def build_layout(clips: Sequence[Clip]) -> TrackLayout:
    """Distribui dispositivos em trilhas. Video primeiro, audio externo depois."""
    layout = TrackLayout()
    by_device: Dict[str, Clip] = {}
    for clip in clips:
        current = by_device.get(clip.device_key)
        if current is None or (clip.has_video and not current.has_video):
            by_device[clip.device_key] = clip

    def sort_key(item: Tuple[str, Clip]):
        key, clip = item
        return (0 if clip.has_video else 1, clip.track_index, key)

    order = 0
    video_slot = 0
    audio_slot = 0
    ordered = sorted(by_device.items(), key=sort_key)
    for key, sample in ordered:
        slot = TrackSlot(device_key=key, label=sample.device_label,
                         kind=sample.device_kind, order=order)
        if sample.has_video:
            video_slot += 1
            slot.video_index = video_slot
        # Todo dispositivo com audio ganha sua propria trilha de audio, para o
        # audio da Sony nunca se misturar com o do Hollyland.
        if any(c.has_audio for c in clips if c.device_key == key):
            audio_slot += 1
            slot.audio_index = audio_slot
        layout.slots[key] = slot
        order += 1
    layout.video_count = video_slot
    layout.audio_count = audio_slot
    return layout


# ----------------------------------------------------------------------
# mapeamento de tempo (fechamento de tempo morto)
# ----------------------------------------------------------------------

@dataclass
class GapEdit:
    at_end: datetime      # instante em que o vazio termina (hora real)
    original: float
    kept: float

    @property
    def removed(self) -> float:
        return max(self.original - self.kept, 0.0)


@dataclass
class TimeMap:
    """Converte hora real de gravacao em segundos na timeline."""

    anchor: datetime
    edits: List[GapEdit] = field(default_factory=list)
    base_offset: float = 0.0

    def seconds(self, when: datetime) -> float:
        value = (when - self.anchor).total_seconds()
        for edit in self.edits:
            if edit.at_end <= when:
                value -= edit.removed
        return max(value, 0.0) + self.base_offset

    @property
    def removed_total(self) -> float:
        return sum(e.removed for e in self.edits)

    def duration_for(self, clips: Sequence[Clip]) -> float:
        end = 0.0
        for clip in clips:
            if clip.start is None:
                continue
            end = max(end, self.seconds(clip.start) + max(clip.duration, 0.0))
        return end


def _kept_for(gap: float, config: Config) -> float:
    if gap < config.gap_min_to_treat_seconds or config.gap_mode == "preserve":
        return gap
    if config.gap_mode == "close":
        return min(gap, max(config.gap_closed_seconds, 0.0))
    # compress
    factor = max(min(config.gap_compress_factor, 1.0), 0.0)
    return max(gap * factor, min(gap, config.gap_closed_seconds))


def build_timemap(clips: Sequence[Clip], config: Config,
                  base_offset: float = 0.0) -> TimeMap:
    """Mapa de tempo para um conjunto de clipes (tipicamente um dia)."""
    ordered = sorted([c for c in clips if c.start], key=lambda c: c.start)
    if not ordered:
        return TimeMap(anchor=datetime.min, base_offset=base_offset)
    tmap = TimeMap(anchor=ordered[0].start, base_offset=base_offset)
    for gap in consolidated_gaps(ordered):
        kept = _kept_for(gap.seconds, config)
        if kept < gap.seconds:
            tmap.edits.append(GapEdit(at_end=gap.at, original=gap.seconds, kept=kept))
    return tmap


# ----------------------------------------------------------------------
# itens posicionados, prontos para exportar ou desenhar
# ----------------------------------------------------------------------

@dataclass
class PlacedClip:
    clip: Clip
    slot: TrackSlot
    timeline_start: float
    duration: float
    label_color: str
    day_index: int
    block_index: int

    @property
    def timeline_end(self) -> float:
        return self.timeline_start + self.duration


@dataclass
class PlacedMarker:
    name: str
    comment: str
    at: float
    color: str = ""
    kind: str = "bloco"        # bloco | dia


@dataclass
class PlacedSequence:
    name: str
    layout: TrackLayout
    clips: List[PlacedClip] = field(default_factory=list)
    markers: List[PlacedMarker] = field(default_factory=list)
    duration: float = 0.0
    day_keys: List[str] = field(default_factory=list)
    removed_seconds: float = 0.0

    @property
    def video_tracks(self) -> int:
        return max([p.slot.video_index for p in self.clips
                    if p.slot.video_index] or [0])

    @property
    def audio_tracks(self) -> int:
        return max([p.slot.audio_index for p in self.clips
                    if p.slot.audio_index] or [0])


def place_day(day: Day, layout: TrackLayout, config: Config,
              base_offset: float = 0.0,
              tmap: Optional[TimeMap] = None) -> Tuple[List[PlacedClip], List[PlacedMarker], float, float]:
    """Posiciona os clipes de um dia e cria os marcadores de bloco."""
    clips = day.clips
    if tmap is None:
        tmap = build_timemap(clips, config, base_offset=base_offset)
    placed: List[PlacedClip] = []
    markers: List[PlacedMarker] = []

    for block in day.blocks:
        color = label_for("bloco", day.index, block.index, 0)
        if block.start is not None:
            markers.append(PlacedMarker(
                name=(f"{block.label()} — "
                      f"{block.start.strftime('%H:%M')} às "
                      f"{block.end.strftime('%H:%M') if block.end else '--:--'} "
                      f"({_dur(block.duration)}, {len(block.clips)} clipes)"),
                comment=(f"{day.label()} {day.key} | "
                         f"{', '.join(f'{k}({v})' for k, v in block.device_counts().items())}"),
                at=tmap.seconds(block.start),
                color=color,
                kind="bloco",
            ))
        for clip in block.clips:
            slot = layout.get(clip.device_key)
            if slot is None or clip.start is None:
                continue
            placed.append(PlacedClip(
                clip=clip,
                slot=slot,
                timeline_start=tmap.seconds(clip.start),
                duration=max(clip.duration, 0.04),
                label_color=label_for(config.color_mode, day.index,
                                      block.index, slot.order),
                day_index=day.index,
                block_index=block.index,
            ))
    duration = max([p.timeline_end for p in placed] or [0.0])
    return placed, markers, duration, tmap.removed_total


def _dur(seconds: float) -> str:
    from .timeutil import format_duration
    return format_duration(seconds)


def build_day_sequence(day: Day, config: Config,
                       layout: Optional[TrackLayout] = None) -> PlacedSequence:
    """Uma sequencia por dia — o padrao. Uma semana numa timeline e ingerivel."""
    layout = layout or build_layout(day.clips)
    placed, markers, duration, removed = place_day(day, layout, config)
    return PlacedSequence(
        name=day.sequence_name(), layout=layout, clips=placed, markers=markers,
        duration=duration, day_keys=[day.key], removed_seconds=removed,
    )


def build_master_sequence(days: Sequence[Day], config: Config,
                          layout: Optional[TrackLayout] = None,
                          name: str = "MASTER — todos os dias") -> PlacedSequence:
    """Sequencia com todos os dias em ordem, para visao geral."""
    all_clips = [c for day in days for c in day.clips]
    layout = layout or build_layout(all_clips)
    seq = PlacedSequence(name=name, layout=layout)
    cursor = 0.0
    for day in days:
        placed, markers, duration, removed = place_day(day, layout, config,
                                                       base_offset=cursor)
        if day.start is not None:
            seq.markers.append(PlacedMarker(
                name=f"=== {day.label()} — {day.key} ===",
                comment=(f"{len(day.blocks)} bloco(s), {len(day.clips)} clipes"),
                at=cursor,
                color=label_for("dia", day.index, 1, 0),
                kind="dia",
            ))
        seq.clips.extend(placed)
        seq.markers.extend(markers)
        seq.removed_seconds += removed
        cursor = duration + MASTER_DAY_GAP
        seq.day_keys.append(day.key)
    seq.duration = max([p.timeline_end for p in seq.clips] or [0.0])
    return seq


def build_sequences(project: Project, config: Config,
                    day_keys: Optional[Sequence[str]] = None,
                    include_master: Optional[bool] = None) -> List[PlacedSequence]:
    """Sequencias a exportar: uma por dia (+ MASTER opcional)."""
    days = [d for d in project.days
            if day_keys is None or d.key in set(day_keys)]
    layout = build_layout([c for d in days for c in d.clips])
    out = [build_day_sequence(day, config, layout) for day in days]
    want_master = config.export_master if include_master is None else include_master
    if want_master and len(days) > 1:
        out.append(build_master_sequence(days, config, layout))
    return out
