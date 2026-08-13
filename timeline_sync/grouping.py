"""Agrupamento hierarquico: DIA -> BLOCO -> TAKE -> CLIPE.

A ordem importa e nao e negociavel: **primeiro separa por dia, depois detecta
blocos dentro de cada dia**. Se a deteccao rodasse sobre a semana inteira, os
intervalos noturnos (12h+) dominariam a estatistica e o algoritmo se limitaria
a separar por dia, ignorando as pausas internas de cada diaria.

A deteccao de blocos e adaptativa (sem limiar fixo): o corte sai da propria
distribuicao dos intervalos daquele dia. Ver DECISOES.md para o comparativo
entre os metodos.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from .config import Config, ProjectState
from .devices import DeviceRegistry
from .models import (TRUST_NONE, Block, Clip, Day, DeviceSummary, Project,
                     Take)
from .timeutil import day_key_for, format_gap

# Quando o dia tem um unico intervalo candidato, exigimos que ele represente
# uma fatia relevante do dia para valer como pausa (evita partir um dia em dois
# por causa de uma troca de lente).
SINGLE_GAP_SPAN_FRACTION = 0.15


# ----------------------------------------------------------------------
# intervalos na linha do tempo consolidada
# ----------------------------------------------------------------------

@dataclass
class GapInfo:
    """Um intervalo sem gravacao entre dois clipes consecutivos."""
    index: int          # indice do clipe que ABRE o proximo trecho
    seconds: float
    at: datetime        # inicio do clipe que abre o trecho


def consolidated_gaps(clips: Sequence[Clip]) -> List[GapInfo]:
    """Intervalos vazios na linha do tempo de TODOS os dispositivos juntos.

    Se a Sony parou mas o iPhone continuou gravando, nao houve pausa real — por
    isso o fim considerado e o maximo acumulado, nao o fim do clipe anterior.
    """
    gaps: List[GapInfo] = []
    if len(clips) < 2:
        return gaps
    running_end = clips[0].end or clips[0].start
    for i in range(1, len(clips)):
        clip = clips[i]
        if clip.start is None:
            continue
        if running_end is not None and clip.start > running_end:
            gaps.append(GapInfo(index=i,
                                seconds=(clip.start - running_end).total_seconds(),
                                at=clip.start))
        end = clip.end
        if end is not None and (running_end is None or end > running_end):
            running_end = end
    return gaps


# ----------------------------------------------------------------------
# algoritmos de corte 1D
# ----------------------------------------------------------------------

@dataclass
class ThresholdDecision:
    threshold: Optional[float]      # None = dia inteiro e um unico bloco
    method: str
    note: str
    score: float = 0.0


def _max_ratio(values: List[float], min_ratio: float) -> Tuple[Optional[int], float]:
    """Maior salto relativo entre intervalos ordenados: `gap[i+1]/gap[i]`.

    Escala-livre por construcao: uma pausa de 20 min e enorme num dia de takes
    de 2 min e irrelevante num dia de takes de 40 min. E exatamente essa
    propriedade que o limiar fixo nao tem.
    """
    best_i: Optional[int] = None
    best = 0.0
    for i in range(len(values) - 1):
        low = max(values[i], 1.0)
        ratio = values[i + 1] / low
        if ratio > best:
            best = ratio
            best_i = i
    if best_i is None or best < min_ratio:
        return None, best
    return best_i, best


def _jenks_break(values: List[float]) -> Tuple[Optional[int], float, float]:
    """Jenks natural breaks com 2 classes (equivalente a k-means 1D exato).

    Devolve (indice do corte, GVF, razao entre as medias das classes).
    Como os valores estao ordenados, testar todos os pontos de corte da o
    otimo global — nao precisa iterar como o k-means classico.
    """
    n = len(values)
    if n < 2:
        return None, 0.0, 0.0
    mean_all = sum(values) / n
    sdam = sum((v - mean_all) ** 2 for v in values)
    if sdam <= 0:
        return None, 0.0, 0.0

    best_i: Optional[int] = None
    best_sdcm = float("inf")
    for i in range(n - 1):
        low = values[:i + 1]
        high = values[i + 1:]
        m1 = sum(low) / len(low)
        m2 = sum(high) / len(high)
        sdcm = sum((v - m1) ** 2 for v in low) + sum((v - m2) ** 2 for v in high)
        if sdcm < best_sdcm:
            best_sdcm = sdcm
            best_i = i
    if best_i is None:
        return None, 0.0, 0.0
    gvf = 1.0 - (best_sdcm / sdam)
    m1 = sum(values[:best_i + 1]) / (best_i + 1)
    m2 = sum(values[best_i + 1:]) / (len(values) - best_i - 1)
    ratio = m2 / m1 if m1 > 0 else float("inf")
    return best_i, gvf, ratio


def _kmeans_break(values: List[float], iterations: int = 40) -> Tuple[Optional[int], float]:
    """k-means 1D com k=2 (Lloyd), partindo dos extremos como centroides."""
    n = len(values)
    if n < 2:
        return None, 0.0
    c1, c2 = values[0], values[-1]
    if c1 == c2:
        return None, 0.0
    split = 0
    for _ in range(iterations):
        mid = (c1 + c2) / 2.0
        split = 0
        while split < n and values[split] <= mid:
            split += 1
        if split == 0 or split == n:
            break
        new_c1 = sum(values[:split]) / split
        new_c2 = sum(values[split:]) / (n - split)
        if abs(new_c1 - c1) < 1e-9 and abs(new_c2 - c2) < 1e-9:
            c1, c2 = new_c1, new_c2
            break
        c1, c2 = new_c1, new_c2
    if split <= 0 or split >= n:
        return None, 0.0
    ratio = c2 / c1 if c1 > 0 else float("inf")
    return split - 1, ratio


def decide_threshold(gaps: List[GapInfo], config: Config, day_span: float,
                     clip_count: int) -> ThresholdDecision:
    """Escolhe o limiar de pausa para UM dia.

    Salvaguardas aplicadas aqui:
      * piso minimo (`block_min_gap_seconds`): intervalos curtos nunca quebram;
      * dia com poucos clipes vira bloco unico (amostra pequena demais);
      * dia uniforme (sem salto claro) vira bloco unico, sem divisao inventada.
    """
    if clip_count < config.block_min_clips_for_split:
        return ThresholdDecision(None, "unico",
                                 f"dia com {clip_count} clipes — amostra pequena, "
                                 "tratado como bloco unico")

    candidates = sorted(g.seconds for g in gaps if g.seconds >= config.block_min_gap_seconds)
    if not candidates:
        return ThresholdDecision(None, "unico",
                                 "nenhuma pausa acima do piso "
                                 f"({format_gap(config.block_min_gap_seconds)}) — bloco unico")

    method = config.block_method

    if method == "fixo":
        return ThresholdDecision(
            max(config.block_fixed_threshold_seconds, config.block_min_gap_seconds),
            "fixo", f"limiar fixo de {format_gap(config.block_fixed_threshold_seconds)}",
        )

    if len(candidates) == 1:
        gap = candidates[0]
        needed = max(config.block_min_gap_seconds,
                     SINGLE_GAP_SPAN_FRACTION * day_span)
        if gap >= needed:
            return ThresholdDecision(gap, method,
                                     f"pausa unica de {format_gap(gap)} — "
                                     f"{gap / day_span * 100:.0f}% do dia, vale corte")
        return ThresholdDecision(None, "unico",
                                 f"pausa unica de {format_gap(gap)} e pequena frente "
                                 f"ao dia ({format_gap(day_span)}) — bloco unico")

    ratio_i, ratio_value = _max_ratio(candidates, config.block_min_ratio)
    jenks_i, gvf, jenks_ratio = _jenks_break(candidates)
    kmeans_i, kmeans_ratio = _kmeans_break(candidates)

    def threshold_from(index: int) -> float:
        low = candidates[index]
        high = candidates[index + 1]
        # media geometrica: fica entre as duas classes em escala logaritmica,
        # que e a escala em que os intervalos de gravacao se distribuem.
        return math.sqrt(max(low, 1.0) * high)

    if method == "ratio":
        if ratio_i is None:
            return ThresholdDecision(None, "unico",
                                     f"maior salto foi {ratio_value:.1f}x, abaixo de "
                                     f"{config.block_min_ratio}x — dia uniforme")
        return ThresholdDecision(threshold_from(ratio_i), "ratio",
                                 f"maior salto {ratio_value:.1f}x", ratio_value)

    if method == "jenks":
        if jenks_i is None or jenks_ratio < config.block_min_ratio or gvf < 0.6:
            return ThresholdDecision(None, "unico",
                                     f"jenks sem separacao clara (GVF {gvf:.2f}, "
                                     f"razao {jenks_ratio:.1f}x) — dia uniforme")
        return ThresholdDecision(threshold_from(jenks_i), "jenks",
                                 f"jenks GVF {gvf:.2f}, razao {jenks_ratio:.1f}x", gvf)

    if method == "kmeans":
        if kmeans_i is None or kmeans_ratio < config.block_min_ratio:
            return ThresholdDecision(None, "unico",
                                     f"k-means sem separacao clara "
                                     f"(razao {kmeans_ratio:.1f}x) — dia uniforme")
        return ThresholdDecision(threshold_from(kmeans_i), "kmeans",
                                 f"k-means razao {kmeans_ratio:.1f}x", kmeans_ratio)

    # --- method == "auto" ---------------------------------------------
    # Razao maxima decide; jenks entra como segunda opiniao quando a razao nao
    # atinge o minimo. Se nenhum dos dois se convence, o dia e uniforme.
    if ratio_i is not None:
        chosen = ratio_i
        # Se o corte de maior razao estoura o teto de blocos, preferimos o
        # maior salto que ainda respeite o teto (em vez de cortar demais e
        # depois mesclar as cegas).
        n_blocks = sum(1 for c in candidates if c >= threshold_from(chosen)) + 1
        if n_blocks > config.block_max_per_day:
            for i in range(len(candidates) - 2, -1, -1):
                low = max(candidates[i], 1.0)
                if candidates[i + 1] / low < config.block_min_ratio:
                    continue
                blocks = sum(1 for c in candidates if c >= threshold_from(i)) + 1
                if blocks <= config.block_max_per_day:
                    chosen = i
                    break
        return ThresholdDecision(
            threshold_from(chosen), "auto/ratio",
            f"maior salto {ratio_value:.1f}x entre intervalos ordenados",
            ratio_value,
        )
    if jenks_i is not None and jenks_ratio >= config.block_min_ratio and gvf >= 0.6:
        return ThresholdDecision(threshold_from(jenks_i), "auto/jenks",
                                 f"razao maxima fraca ({ratio_value:.1f}x), jenks "
                                 f"separou (GVF {gvf:.2f}, {jenks_ratio:.1f}x)", gvf)
    return ThresholdDecision(
        None, "unico",
        f"intervalos do dia sao parecidos (maior salto {ratio_value:.1f}x) — "
        "bloco unico, sem divisao artificial",
    )


# ----------------------------------------------------------------------
# montagem de blocos
# ----------------------------------------------------------------------

def _boundaries_from_threshold(gaps: List[GapInfo], threshold: float,
                               floor: float) -> List[GapInfo]:
    return [g for g in gaps if g.seconds >= max(threshold, floor)]


def _merge_to_cap(boundaries: List[GapInfo], max_blocks: int) -> Tuple[List[GapInfo], int]:
    """Mescla iterativamente pelos menores intervalos ate respeitar o teto."""
    merged = 0
    kept = sorted(boundaries, key=lambda g: g.index)
    while len(kept) + 1 > max_blocks and kept:
        smallest = min(kept, key=lambda g: g.seconds)
        kept.remove(smallest)
        merged += 1
    return kept, merged


def group_takes(clips: List[Clip], window: float) -> List[Take]:
    """Agrupa clipes de dispositivos diferentes iniciados quase juntos.

    Um segundo clipe do MESMO dispositivo abre um take novo: se a Sony gravou
    dois arquivos, sao duas tomadas (ou uma gravacao dividida), nao o mesmo
    take capturado duas vezes.
    """
    takes: List[Take] = []
    current: Optional[Take] = None
    anchor: Optional[datetime] = None
    for clip in clips:
        if clip.start is None:
            continue
        starts_new = (
            current is None
            or anchor is None
            or (clip.start - anchor).total_seconds() > window
            or clip.device_key in {c.device_key for c in current.clips}
        )
        if starts_new:
            current = Take(index=len(takes) + 1)
            takes.append(current)
            anchor = clip.start
        current.clips.append(clip)
        clip.take_index = current.index
    return takes


def build_day(key: str, index: int, clips: List[Clip], config: Config,
              state: Optional[ProjectState] = None) -> Day:
    """Monta um dia: detecta blocos e agrupa takes dentro de cada bloco."""
    clips = sorted(clips, key=lambda c: (c.start or datetime.min, c.filename))
    day = Day(index=index, key=key)
    for clip in clips:
        clip.day_key = key

    span = 0.0
    if clips and clips[0].start:
        last_end = max((c.end for c in clips if c.end), default=clips[0].start)
        span = (last_end - clips[0].start).total_seconds()

    gaps = consolidated_gaps(clips)

    manual = None
    source = "auto"
    if state is not None and key in state.day_thresholds:
        manual = state.day_thresholds[key]
        source = "manual"
    elif config.block_manual_threshold_all_days is not None:
        manual = config.block_manual_threshold_all_days
        source = "global"

    if manual is not None:
        threshold = max(float(manual), config.block_min_gap_seconds)
        note = f"limiar manual de {format_gap(threshold)}"
        if source == "global":
            note += " (aplicado a todos os dias)"
        decision = ThresholdDecision(threshold, source, note)
    else:
        decision = decide_threshold(gaps, config, span or 1.0, len(clips))
        source = decision.method

    if decision.threshold is None:
        boundaries: List[GapInfo] = []
    else:
        boundaries = _boundaries_from_threshold(gaps, decision.threshold,
                                                config.block_min_gap_seconds)

    # ajustes manuais salvos por projeto
    note_extra: List[str] = []
    if state is not None:
        removed = set(state.merged_boundaries.get(key, []))
        forced = set(state.forced_splits.get(key, []))
        if removed:
            before = len(boundaries)
            boundaries = [g for g in boundaries if g.at.isoformat() not in removed]
            if len(boundaries) != before:
                note_extra.append(f"{before - len(boundaries)} fronteira(s) mesclada(s) na mao")
        if forced:
            existing = {g.at.isoformat() for g in boundaries}
            added = 0
            for g in gaps:
                iso = g.at.isoformat()
                if iso in forced and iso not in existing:
                    boundaries.append(g)
                    added += 1
            # divisao manual pode ser pedida onde nao havia intervalo registrado
            for i, clip in enumerate(clips):
                if i == 0 or clip.start is None:
                    continue
                iso = clip.start.isoformat()
                if iso in forced and iso not in {g.at.isoformat() for g in boundaries}:
                    boundaries.append(GapInfo(index=i, seconds=0.0, at=clip.start))
                    added += 1
            if added:
                note_extra.append(f"{added} divisao(oes) forcada(s) na mao")
            boundaries.sort(key=lambda g: g.index)

    boundaries, merged = _merge_to_cap(boundaries, max(1, config.block_max_per_day))
    if merged:
        note_extra.append(
            f"teto de {config.block_max_per_day} blocos/dia acionado: "
            f"{merged} fronteira(s) mesclada(s) pelos menores intervalos"
        )

    day.threshold_seconds = decision.threshold
    day.threshold_source = source
    day.detection_note = "; ".join([decision.note] + note_extra)

    cut_indexes = sorted(g.index for g in boundaries)
    slices: List[List[Clip]] = []
    start = 0
    for cut in cut_indexes:
        slices.append(clips[start:cut])
        start = cut
    slices.append(clips[start:])

    for i, chunk in enumerate([s for s in slices if s], start=1):
        block = Block(index=i)
        for clip in chunk:
            clip.block_index = i
        block.takes = group_takes(chunk, config.take_window_seconds)
        day.blocks.append(block)
    return day


# ----------------------------------------------------------------------
# projeto completo
# ----------------------------------------------------------------------

def detect_duplicates(clips: Sequence[Clip]) -> List[List[Clip]]:
    """Mesmo arquivo importado duas vezes: nome + tamanho iguais, caminhos diferentes."""
    buckets: Dict[Tuple[str, int], List[Clip]] = defaultdict(list)
    for clip in clips:
        buckets[(clip.filename.lower(), clip.size)].append(clip)
    return [group for group in buckets.values() if len(group) > 1]


def sanity_check_devices(days: List[Day], devices: List[DeviceSummary]) -> List[str]:
    """Alerta quando um dispositivo aparece em dias onde ninguem mais gravou.

    Sinal quase certo de offset errado ou relogio zerado no meio da producao —
    o tipo de erro que passa despercebido e estraga a organizacao inteira.
    """
    warnings: List[str] = []
    day_devices: Dict[str, set] = {}
    for day in days:
        day_devices[day.key] = {c.device_key for c in day.clips}

    for summary in devices:
        solo_days = [k for k, keys in day_devices.items()
                     if keys == {summary.key}]
        if not solo_days:
            continue
        others = [k for k, keys in day_devices.items() if summary.key not in keys and keys]
        if others:
            msg = (
                f"ALERTA: {summary.label} e o UNICO dispositivo em "
                f"{', '.join(sorted(solo_days))}, enquanto outros aparelhos gravaram "
                f"em {', '.join(sorted(others)[:3])}. Isso costuma indicar offset "
                f"errado ou relogio zerado. Confira a calibracao antes de exportar."
            )
            warnings.append(msg)
            summary.warnings.append("aparece isolado em " + ", ".join(sorted(solo_days)))

    # Dispositivo cujo intervalo de datas nao intersecta o de ninguem
    spans: Dict[str, Tuple[datetime, datetime]] = {}
    for summary in devices:
        clips = [c for day in days for c in day.clips if c.device_key == summary.key]
        starts = [c.start for c in clips if c.start]
        if starts:
            spans[summary.key] = (min(starts), max(starts))
    for key, (lo, hi) in spans.items():
        overlaps = any(
            other != key and not (hi < olo or ohi < lo)
            for other, (olo, ohi) in spans.items()
        )
        if not overlaps and len(spans) > 1:
            label = next((d.label for d in devices if d.key == key), key)
            warnings.append(
                f"ALERTA: as datas de {label} ({lo.date()} a {hi.date()}) nao se "
                "cruzam com nenhum outro dispositivo. Offset provavelmente errado."
            )
    return warnings


def build_project(clips: List[Clip], config: Config,
                  registry: Optional[DeviceRegistry] = None,
                  state: Optional[ProjectState] = None) -> Project:
    """Organiza a lista de clipes na hierarquia completa."""
    project = Project()
    project.total_files = len(clips)
    project.total_bytes = sum(c.size for c in clips)
    project.bytes_read = sum(c.bytes_read for c in clips)

    dated: List[Clip] = []
    for clip in clips:
        if clip.start is None:
            clip.trust = TRUST_NONE
            project.unknown_time_clips.append(clip)
        else:
            dated.append(clip)
        if clip.fallback:
            project.fallback_clips.append(clip)
        method = clip.read_method or "?"
        project.read_methods[method] = project.read_methods.get(method, 0) + 1

    # 1) separa por dia — SEMPRE antes de detectar blocos
    by_day: Dict[str, List[Clip]] = defaultdict(list)
    for clip in dated:
        by_day[day_key_for(clip.start, config.day_start_hour)].append(clip)

    # 2) blocos dentro de cada dia, independentemente
    for index, key in enumerate(sorted(by_day), start=1):
        project.days.append(build_day(key, index, by_day[key], config, state))

    # 3) resumo por dispositivo
    counts: Dict[str, int] = defaultdict(int)
    meta: Dict[str, Clip] = {}
    tz_known: Dict[str, bool] = {}
    for clip in clips:
        counts[clip.device_key] += 1
        meta.setdefault(clip.device_key, clip)
        tz_known[clip.device_key] = tz_known.get(clip.device_key, True) and clip.tz_known
    for key, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        sample = meta[key]
        profile = registry.get(key) if registry else None
        project.devices.append(DeviceSummary(
            key=key,
            kind=sample.device_kind,
            label=sample.device_label,
            make=sample.make,
            model=sample.model,
            clip_count=count,
            offset_seconds=profile.total_offset if profile else 0.0,
            calibrated=bool(profile and profile.calibrated),
            calibrated_at=profile.calibrated_at if profile else "",
            track_index=sample.track_index,
            is_reference=tz_known.get(key, False),
        ))

    project.duplicate_groups = detect_duplicates(clips)
    project.warnings.extend(sanity_check_devices(project.days, project.devices))

    uncalibrated = [d for d in project.devices
                    if not d.calibrated and not d.is_reference]
    for dev in uncalibrated:
        project.warnings.append(
            f"{dev.label} nao esta calibrado. O horario dele vem do relogio interno "
            "e pode estar errado por horas. Cadastre com um clipe do iPhone."
        )
    if project.unknown_time_clips:
        project.warnings.append(
            f"{len(project.unknown_time_clips)} arquivo(s) sem data utilizavel — "
            "ficaram fora da timeline e precisam de resolucao manual."
        )
    if project.duplicate_groups:
        project.warnings.append(
            f"{len(project.duplicate_groups)} grupo(s) de arquivos duplicados "
            "(mesmo nome e tamanho em pastas diferentes)."
        )
    orphans = project.orphan_takes()
    if orphans:
        project.warnings.append(
            f"{len(orphans)} take(s) cobertos por um unico dispositivo — "
            "geralmente falta de audio externo ou camera que nao rodou."
        )
    return project
