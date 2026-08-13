"""Exportacao em Final Cut Pro XML 7 (`xmeml` v4) — formato que o Premiere
importa nativamente.

Decisoes registradas em DECISOES.md; as principais aqui:

* O XML e montado como texto. Nao existe biblioteca confiavel para xmeml e o
  schema e simples o bastante para gerar na mao com seguranca.
* Uma sequencia por dia (padrao) + uma sequencia MASTER opcional.
* `<labels><label2>` carrega a cor, restrita as 16 labels validas do Premiere.
* Bins espelham a hierarquia DIA / BLOCO no painel de projeto.
* Marcadores demarcam blocos (e dias, na MASTER). O xmeml nao tem campo de cor
  de marcador que o Premiere respeite — a cor do bloco vai no comentario.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote
from xml.sax.saxutils import escape

from .colors import is_valid
from .config import Config
from .models import Clip, Day, Project
from .timeline import PlacedClip, PlacedSequence


def frames(seconds: float, timebase: int) -> int:
    return max(int(round(seconds * timebase)), 0)


def path_to_url(path: str, root_from: str = "", root_to: str = "") -> str:
    """Converte caminho de arquivo em `pathurl` no formato que o Premiere aceita.

    O xmeml nao tem suporte confiavel a caminho relativo no Premiere, entao o
    caminho e absoluto. Para sobreviver a mudanca de ponto de montagem (o G: do
    Google Drive virar H:), a interface tem o campo "raiz do projeto": trocando
    a raiz e reexportando, o XML sai apontando para o lugar novo sem relink
    manual de centenas de clipes.
    """
    absolute = os.path.abspath(path)
    if root_from and root_to:
        norm_from = os.path.normpath(root_from)
        norm_abs = os.path.normpath(absolute)
        if norm_abs.lower().startswith(norm_from.lower()):
            absolute = os.path.join(root_to, os.path.relpath(norm_abs, norm_from))
    unified = absolute.replace("\\", "/")
    if sys.platform.startswith("win") or (len(unified) > 1 and unified[1] == ":"):
        # G:/Meu Drive/... -> file://localhost/G:/Meu%20Drive/...
        return "file://localhost/" + quote(unified, safe="/:")
    return "file://localhost" + quote(unified, safe="/:")


@dataclass
class _Ids:
    """Gerador de ids estaveis por arquivo/clipe."""
    file_ids: Dict[str, str] = field(default_factory=dict)
    master_ids: Dict[str, str] = field(default_factory=dict)
    counter: int = 0

    def file_id(self, path: str) -> Tuple[str, bool]:
        """Retorna (id, primeira_vez). Somente a 1a ocorrencia descreve o arquivo."""
        if path in self.file_ids:
            return self.file_ids[path], False
        self.counter += 1
        fid = f"file-{self.counter}"
        self.file_ids[path] = fid
        return fid, True

    def master_id(self, path: str) -> str:
        if path not in self.master_ids:
            self.master_ids[path] = f"masterclip-{len(self.master_ids) + 1}"
        return self.master_ids[path]

    def next(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}-{self.counter}"


class _Xml:
    """Acumulador de linhas com indentacao. Mais legivel que ElementTree aqui."""

    def __init__(self):
        self.lines: List[str] = []
        self.depth = 0

    def open(self, tag: str, attrs: str = "") -> "_Xml":
        space = f" {attrs}" if attrs else ""
        self.lines.append("  " * self.depth + f"<{tag}{space}>")
        self.depth += 1
        return self

    def close(self, tag: str) -> "_Xml":
        self.depth = max(self.depth - 1, 0)
        self.lines.append("  " * self.depth + f"</{tag}>")
        return self

    def leaf(self, tag: str, value) -> "_Xml":
        text = escape(str(value)) if not isinstance(value, bool) else ("TRUE" if value else "FALSE")
        self.lines.append("  " * self.depth + f"<{tag}>{text}</{tag}>")
        return self

    def raw(self, text: str) -> "_Xml":
        self.lines.append("  " * self.depth + text)
        return self

    def render(self) -> str:
        return "\n".join(self.lines) + "\n"


def _rate(x: _Xml, timebase: int) -> None:
    x.open("rate").leaf("timebase", timebase).leaf("ntsc", "FALSE").close("rate")


def _sample_characteristics(x: _Xml, width: int, height: int, timebase: int) -> None:
    x.open("samplecharacteristics")
    _rate(x, timebase)
    x.leaf("width", width).leaf("height", height)
    x.leaf("anamorphic", "FALSE").leaf("pixelaspectratio", "square")
    x.leaf("fielddominance", "none")
    x.close("samplecharacteristics")


def _file_element(x: _Xml, clip: Clip, fid: str, first: bool, config: Config,
                  timebase: int) -> None:
    if not first:
        x.raw(f'<file id="{fid}"/>')
        return
    x.open("file", f'id="{fid}"')
    x.leaf("name", clip.filename)
    x.leaf("pathurl", path_to_url(clip.path, config.media_root_original,
                                  config.media_root_override))
    _rate(x, timebase)
    x.leaf("duration", frames(clip.duration, timebase))
    x.open("timecode")
    _rate(x, timebase)
    x.leaf("string", "00:00:00:00").leaf("frame", 0).leaf("displayformat", "NDF")
    x.close("timecode")
    x.open("media")
    if clip.has_video:
        x.open("video")
        x.leaf("duration", frames(clip.duration, timebase))
        _sample_characteristics(x, clip.width or config.sequence_width,
                                clip.height or config.sequence_height, timebase)
        x.close("video")
    if clip.has_audio:
        x.open("audio")
        x.open("samplecharacteristics").leaf("depth", 16).leaf("samplerate", 48000)
        x.close("samplecharacteristics")
        x.leaf("channelcount", 2)
        x.close("audio")
    x.close("media")
    x.close("file")


def _labels(x: _Xml, color: str) -> None:
    if color and is_valid(color):
        x.open("labels").leaf("label2", color).close("labels")


def _clipitem(x: _Xml, placed: PlacedClip, ids: _Ids, config: Config,
              timebase: int, media_type: str, item_id: str,
              link_group: Sequence[Tuple[str, str, int, int]],
              source_track: int = 1) -> None:
    clip = placed.clip
    start = frames(placed.timeline_start, timebase)
    length = max(frames(placed.duration, timebase), 1)
    fid, first = ids.file_id(clip.path)

    x.open("clipitem", f'id="{item_id}"')
    x.leaf("masterclipid", ids.master_id(clip.path))
    x.leaf("name", clip.filename)
    x.leaf("enabled", "TRUE")
    x.leaf("duration", frames(clip.duration, timebase))
    _rate(x, timebase)
    x.leaf("start", start)
    x.leaf("end", start + length)
    x.leaf("in", 0)
    x.leaf("out", length)
    x.leaf("pproTicksIn", 0)
    x.leaf("pproTicksOut", int(length / timebase * 254016000000))
    x.leaf("alphatype", "none")
    _file_element(x, clip, fid, first, config, timebase)
    if media_type == "audio":
        x.open("sourcetrack").leaf("mediatype", "audio")
        x.leaf("trackindex", source_track).close("sourcetrack")
    else:
        x.open("sourcetrack").leaf("mediatype", "video")
        x.leaf("trackindex", 1).close("sourcetrack")
    _labels(x, placed.label_color)
    x.open("comments")
    x.leaf("mastercomment1",
           f"DIA {placed.day_index:02d} / BLOCO {placed.block_index:02d}")
    x.leaf("mastercomment2",
           f"{clip.device_label} | hora real {clip.start.strftime('%d/%m %H:%M:%S') if clip.start else '?'}")
    x.leaf("mastercomment3",
           f"lido via {clip.read_method} | confianca {clip.trust}"
           + (" | FALLBACK" if clip.fallback else ""))
    x.close("comments")
    for ref, mtype, tindex, cindex in link_group:
        x.open("link")
        x.leaf("linkclipref", ref)
        x.leaf("mediatype", mtype)
        x.leaf("trackindex", tindex)
        x.leaf("clipindex", cindex)
        x.close("link")
    x.close("clipitem")


def _sequence(x: _Xml, seq: PlacedSequence, ids: _Ids, config: Config) -> None:
    timebase = config.sequence_timebase
    seq_id = ids.next("sequence")
    total = max(frames(seq.duration, timebase), 1)

    x.open("sequence", f'id="{seq_id}"')
    x.leaf("name", seq.name)
    x.leaf("duration", total)
    _rate(x, timebase)
    x.open("timecode")
    _rate(x, timebase)
    x.leaf("string", "00:00:00:00").leaf("frame", 0).leaf("displayformat", "NDF")
    x.close("timecode")
    x.open("media")

    # --- video ---------------------------------------------------------
    x.open("video")
    x.open("format")
    _sample_characteristics(x, config.sequence_width, config.sequence_height, timebase)
    x.close("format")

    video_tracks = max([p.slot.video_index for p in seq.clips if p.slot.video_index] or [1])
    audio_tracks = max([p.slot.audio_index for p in seq.clips if p.slot.audio_index] or [1])

    # ids reservados antes de escrever, para os <link> apontarem corretamente
    item_ids: Dict[int, Dict[str, str]] = {}
    counters = {"video": {}, "audio": {}}
    for idx, placed in enumerate(seq.clips):
        entry: Dict[str, str] = {}
        if placed.slot.video_index and placed.clip.has_video:
            t = placed.slot.video_index
            counters["video"][t] = counters["video"].get(t, 0) + 1
            entry["video"] = ids.next("clipitem")
            entry["video_track"] = str(t)
            entry["video_index"] = str(counters["video"][t])
        if placed.slot.audio_index and placed.clip.has_audio:
            t = placed.slot.audio_index
            counters["audio"][t] = counters["audio"].get(t, 0) + 1
            entry["audio"] = ids.next("clipitem")
            entry["audio_track"] = str(t)
            entry["audio_index"] = str(counters["audio"][t])
        item_ids[idx] = entry

    def links_for(entry: Dict[str, str]) -> List[Tuple[str, str, int, int]]:
        out: List[Tuple[str, str, int, int]] = []
        if "video" in entry:
            out.append((entry["video"], "video", int(entry["video_track"]),
                        int(entry["video_index"])))
        if "audio" in entry:
            out.append((entry["audio"], "audio", int(entry["audio_track"]),
                        int(entry["audio_index"])))
        return out if len(out) > 1 else []

    for track in range(1, video_tracks + 1):
        x.open("track")
        for idx, placed in enumerate(seq.clips):
            entry = item_ids[idx]
            if entry.get("video_track") != str(track):
                continue
            _clipitem(x, placed, ids, config, timebase, "video",
                      entry["video"], links_for(entry))
        x.leaf("enabled", "TRUE")
        x.leaf("locked", "FALSE")
        x.close("track")
    x.close("video")

    # --- audio ---------------------------------------------------------
    x.open("audio")
    x.open("format")
    x.open("samplecharacteristics").leaf("depth", 16).leaf("samplerate", 48000)
    x.close("samplecharacteristics")
    x.close("format")
    for track in range(1, audio_tracks + 1):
        x.open("track")
        for idx, placed in enumerate(seq.clips):
            entry = item_ids[idx]
            if entry.get("audio_track") != str(track):
                continue
            _clipitem(x, placed, ids, config, timebase, "audio",
                      entry["audio"], links_for(entry))
        x.leaf("enabled", "TRUE")
        x.leaf("locked", "FALSE")
        x.leaf("outputchannelindex", track)
        x.close("track")
    x.close("audio")
    x.close("media")

    # --- marcadores ----------------------------------------------------
    for marker in seq.markers:
        x.open("marker")
        x.leaf("name", marker.name)
        comment = marker.comment
        if marker.color:
            comment = f"{comment} | cor: {marker.color}" if comment else f"cor: {marker.color}"
        x.leaf("comment", comment)
        x.leaf("in", frames(marker.at, timebase))
        x.leaf("out", -1)
        x.close("marker")

    x.close("sequence")


def _master_clip(x: _Xml, clip: Clip, ids: _Ids, config: Config, color: str,
                 timebase: int) -> None:
    """Master clip para o painel de projeto (dentro de um bin)."""
    fid, first = ids.file_id(clip.path)
    mid = ids.master_id(clip.path)
    x.open("clip", f'id="{mid}"')
    x.leaf("name", clip.filename)
    x.leaf("duration", frames(clip.duration, timebase))
    _rate(x, timebase)
    x.leaf("in", -1)
    x.leaf("out", -1)
    x.leaf("masterclipid", mid)
    x.leaf("ismasterclip", "TRUE")
    _labels(x, color)
    x.open("logginginfo")
    x.leaf("description", f"{clip.device_label}")
    x.leaf("scene", clip.day_key)
    x.leaf("shottake", f"BLOCO {clip.block_index:02d} / TAKE {clip.take_index:02d}")
    x.leaf("lognote",
           f"hora real {clip.start.strftime('%Y-%m-%d %H:%M:%S') if clip.start else '?'}"
           f" | offset {clip.offset_seconds:+.0f}s | fonte {clip.time_source}")
    x.close("logginginfo")
    x.open("media")
    if clip.has_video:
        x.open("video")
        x.open("track")
        x.open("clipitem", f'id="{ids.next("clipitem")}"')
        x.leaf("name", clip.filename)
        x.leaf("duration", frames(clip.duration, timebase))
        _rate(x, timebase)
        x.leaf("in", 0)
        x.leaf("out", max(frames(clip.duration, timebase), 1))
        _file_element(x, clip, fid, first, config, timebase)
        first = False
        x.close("clipitem")
        x.close("track")
        x.close("video")
    if clip.has_audio:
        x.open("audio")
        x.open("track")
        x.open("clipitem", f'id="{ids.next("clipitem")}"')
        x.leaf("name", clip.filename)
        x.leaf("duration", frames(clip.duration, timebase))
        _rate(x, timebase)
        x.leaf("in", 0)
        x.leaf("out", max(frames(clip.duration, timebase), 1))
        _file_element(x, clip, fid, first, config, timebase)
        x.open("sourcetrack").leaf("mediatype", "audio").leaf("trackindex", 1)
        x.close("sourcetrack")
        x.close("clipitem")
        x.close("track")
        x.close("audio")
    x.close("media")
    x.close("clip")


def _bins(x: _Xml, days: Sequence[Day], ids: _Ids, config: Config) -> None:
    """Bins espelhando DIA / BLOCO no painel de projeto do Premiere."""
    from .colors import label_for
    timebase = config.sequence_timebase
    for day in days:
        x.open("bin")
        x.leaf("name", day.sequence_name())
        x.open("children")
        for block in day.blocks:
            x.open("bin")
            x.leaf("name", f"{block.label()} "
                           f"({block.start.strftime('%H:%M') if block.start else '--:--'}"
                           f"-{block.end.strftime('%H:%M') if block.end else '--:--'})")
            x.open("children")
            for clip in block.clips:
                color = label_for(config.color_mode, day.index, block.index,
                                  clip.track_index)
                _master_clip(x, clip, ids, config, color, timebase)
            x.close("children")
            x.close("bin")
        x.close("children")
        x.close("bin")


def build_xml(project: Project, sequences: Sequence[PlacedSequence],
              config: Config, project_name: str = "Timeline Sync",
              include_bins: bool = True,
              days: Optional[Sequence[Day]] = None) -> str:
    """Monta o documento xmeml completo."""
    ids = _Ids()
    x = _Xml()
    x.raw('<?xml version="1.0" encoding="UTF-8"?>')
    x.raw("<!DOCTYPE xmeml>")
    x.open("xmeml", 'version="4"')
    x.open("project")
    x.leaf("name", project_name)
    x.open("children")
    if include_bins:
        _bins(x, days if days is not None else project.days, ids, config)
    for seq in sequences:
        _sequence(x, seq, ids, config)
    x.close("children")
    x.close("project")
    x.close("xmeml")
    return x.render()


@dataclass
class ExportResult:
    files: List[str] = field(default_factory=list)
    sequences: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    clip_count: int = 0


def export(project: Project, config: Config, out_dir: str,
           day_keys: Optional[Sequence[str]] = None,
           include_master: Optional[bool] = None,
           one_file_per_day: bool = True,
           project_name: str = "Timeline Sync") -> ExportResult:
    """Gera os XMLs. Por padrao, um arquivo por dia + um MASTER.

    `day_keys` permite exportar so um dia ou um intervalo de dias.
    """
    from .timeline import build_layout, build_day_sequence, build_master_sequence

    os.makedirs(out_dir, exist_ok=True)
    result = ExportResult()
    days = [d for d in project.days if day_keys is None or d.key in set(day_keys)]
    if not days:
        result.warnings.append("nenhum dia selecionado para exportar")
        return result

    layout = build_layout([c for d in days for c in d.clips])
    want_master = config.export_master if include_master is None else include_master

    if one_file_per_day:
        for day in days:
            seq = build_day_sequence(day, config, layout)
            xml = build_xml(project, [seq], config,
                            project_name=f"{project_name} — {day.sequence_name()}",
                            days=[day])
            dest = os.path.join(out_dir, f"{_safe(day.label())}_{day.key}.xml")
            _write(dest, xml)
            result.files.append(dest)
            result.sequences.append(seq.name)
            result.clip_count += len(seq.clips)
        if want_master and len(days) > 1:
            seq = build_master_sequence(days, config, layout)
            xml = build_xml(project, [seq], config,
                            project_name=f"{project_name} — MASTER", days=days)
            dest = os.path.join(out_dir, "MASTER_todos_os_dias.xml")
            _write(dest, xml)
            result.files.append(dest)
            result.sequences.append(seq.name)
    else:
        seqs = [build_day_sequence(day, config, layout) for day in days]
        if want_master and len(days) > 1:
            seqs.append(build_master_sequence(days, config, layout))
        xml = build_xml(project, seqs, config, project_name=project_name, days=days)
        dest = os.path.join(out_dir, f"{_safe(project_name)}.xml")
        _write(dest, xml)
        result.files.append(dest)
        result.sequences.extend(s.name for s in seqs)
        result.clip_count = sum(len(s.clips) for s in seqs)

    if project.unknown_time_clips:
        result.warnings.append(
            f"{len(project.unknown_time_clips)} arquivo(s) sem data ficaram fora "
            "dos XMLs."
        )
    return result


def _safe(name: str) -> str:
    out = "".join(c if c.isalnum() or c in "-_ " else "_" for c in name)
    return out.strip().replace(" ", "_") or "sequencia"


def _write(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
