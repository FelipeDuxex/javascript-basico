"""Utilidades de tempo e formatacao."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

_FALLBACK_TZ = timezone(timedelta(hours=-3))  # horario de Brasilia


def get_tz(name: str):
    """Resolve um fuso por nome, caindo para -03:00 se o tzdata nao existir."""
    if not name:
        return _FALLBACK_TZ
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return _FALLBACK_TZ


def to_local_naive(dt: Optional[datetime], tzname: str) -> Optional[datetime]:
    """Converte para o fuso do projeto e remove o tzinfo.

    Todo o pipeline trabalha em "hora de parede local", que e como a producao
    pensa ("gravamos das 10 as 12"). Clipes com fuso explicito sao convertidos;
    clipes sem fuso sao tratados como ja estando na hora local.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(get_tz(tzname)).replace(tzinfo=None)


def day_key_for(dt: datetime, day_start_hour: float) -> str:
    """Data-calendario da gravacao, com virada de dia configuravel.

    Com `day_start_hour=4`, uma gravacao que terminou 01:30 conta como o dia
    anterior — que e o dia em que a equipe realmente estava trabalhando.
    """
    shifted = dt - timedelta(hours=day_start_hour)
    return shifted.date().isoformat()


def format_offset(seconds: float) -> str:
    """+3h02min14s / -00min08s"""
    sign = "-" if seconds < 0 else "+"
    total = int(round(abs(seconds)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{sign}{h}h{m:02d}min{s:02d}s"
    if m:
        return f"{sign}{m}min{s:02d}s"
    return f"{sign}{s}s"


def format_duration(seconds: float) -> str:
    """2h11 / 45min / 12s"""
    total = int(round(max(seconds, 0)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}"
    if m:
        return f"{m}min"
    return f"{s}s"


def format_gap(seconds: float) -> str:
    total = int(round(max(seconds, 0)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}min"
    if m:
        return f"{m} min"
    return f"{s} s"


def format_bytes(n: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(max(n, 0))
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def hhmm(dt: Optional[datetime]) -> str:
    return dt.strftime("%H:%M") if dt else "--:--"


def hhmmss(dt: Optional[datetime]) -> str:
    return dt.strftime("%H:%M:%S") if dt else "--:--:--"
