"""Refinamento opcional por forma de onda.

O metodo principal do app e metadado — e o que escala para centenas de arquivos.
Isto aqui e refino fino, para dois casos:

1. calibracao de camera: achar o pico (palma/claquete) em cada clipe e zerar
   tambem o erro de segundos;
2. botao "Refinar sync por audio": correlacao cruzada entre clipes que ja foram
   agrupados no mesmo take por horario, ajustando o encaixe exato.

Depende de `ffmpeg` para decodificar o audio. `numpy` acelera a correlacao mas
nao e obrigatorio — sem ele usamos uma versao mais lenta em Python puro sobre
uma janela reduzida.
"""

from __future__ import annotations

import array
import shutil
import subprocess
from dataclasses import dataclass
from typing import List, Optional, Tuple

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
SAMPLE_RATE = 8000       # suficiente para transientes; mantem o custo baixo


def available() -> bool:
    return shutil.which(FFMPEG) is not None


def _numpy():
    try:
        import numpy
        return numpy
    except ImportError:
        return None


def decode_mono(path: str, seconds: float = 30.0,
                start: float = 0.0) -> Optional[array.array]:
    """Decodifica um trecho de audio para PCM mono 16-bit via ffmpeg."""
    if not available():
        return None
    cmd = [
        FFMPEG, "-v", "error", "-ss", f"{start:.3f}", "-t", f"{seconds:.3f}",
        "-i", path, "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
        "-f", "s16le", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    data = array.array("h")
    usable = len(proc.stdout) - (len(proc.stdout) % 2)
    data.frombytes(proc.stdout[:usable])
    return data


def envelope(samples: array.array, window: int = 80) -> List[float]:
    """Envelope de energia — o que importa para transientes, nao a fase."""
    out: List[float] = []
    total = 0.0
    for i, value in enumerate(samples):
        total += abs(value)
        if (i + 1) % window == 0:
            out.append(total / window)
            total = 0.0
    return out


def peak_offset(path: str, seconds: float = 30.0) -> Optional[float]:
    """Instante (em segundos do inicio do clipe) do pico sonoro mais forte.

    Usado na calibracao: se os dois clipes registraram a mesma palma, o pico da
    a referencia exata e o erro de segundos vai a zero.
    """
    samples = decode_mono(path, seconds=seconds)
    if not samples:
        return None
    window = 80
    env = envelope(samples, window)
    if not env:
        return None
    peak_index = max(range(len(env)), key=lambda i: env[i])
    mean = sum(env) / len(env)
    if mean <= 0 or env[peak_index] < mean * 3:
        # Sem transiente destacado (ex.: som ambiente uniforme): nao inventa.
        return None
    return peak_index * window / SAMPLE_RATE


@dataclass
class SyncRefinement:
    offset_seconds: float
    confidence: float
    method: str

    def to_dict(self):
        return {"offset_seconds": self.offset_seconds,
                "confidence": self.confidence, "method": self.method}


def cross_correlate(reference: str, other: str, window_seconds: float = 30.0,
                    max_shift_seconds: float = 10.0) -> Optional[SyncRefinement]:
    """Deslocamento de `other` em relacao a `reference` por correlacao cruzada.

    Positivo = `other` comeca depois. Usa envelope de energia, que e robusto a
    diferenca de microfone/ganho entre um iPhone e um Hollyland.
    """
    a = decode_mono(reference, seconds=window_seconds)
    b = decode_mono(other, seconds=window_seconds)
    if not a or not b:
        return None
    window = 40
    ea = envelope(a, window)
    eb = envelope(b, window)
    if len(ea) < 8 or len(eb) < 8:
        return None
    step = window / SAMPLE_RATE
    max_shift = int(max_shift_seconds / step)

    np = _numpy()
    if np is not None:
        va = np.asarray(ea, dtype=float)
        vb = np.asarray(eb, dtype=float)
        va -= va.mean()
        vb -= vb.mean()
        if va.std() == 0 or vb.std() == 0:
            return None
        full = np.correlate(va, vb, mode="full")
        center = len(vb) - 1
        lo = max(center - max_shift, 0)
        hi = min(center + max_shift + 1, len(full))
        segment = full[lo:hi]
        best = int(np.argmax(segment)) + lo
        peak = float(segment.max())
        norm = float(np.sqrt((va ** 2).sum() * (vb ** 2).sum())) or 1.0
        return SyncRefinement((best - center) * step, peak / norm, "numpy")

    # Python puro: janela limitada para nao ficar caro.
    mean_a = sum(ea) / len(ea)
    mean_b = sum(eb) / len(eb)
    va = [v - mean_a for v in ea]
    vb = [v - mean_b for v in eb]
    best_shift = 0
    best_score = float("-inf")
    for shift in range(-max_shift, max_shift + 1):
        score = 0.0
        count = 0
        for i, value in enumerate(vb):
            j = i + shift
            if 0 <= j < len(va):
                score += va[j] * value
                count += 1
        if count < 8:
            continue
        score /= count
        if score > best_score:
            best_score = score
            best_shift = shift
    denom = (sum(v * v for v in va) * sum(v * v for v in vb)) ** 0.5 or 1.0
    return SyncRefinement(best_shift * step, best_score * len(vb) / denom, "puro")


def refine_take(clips, window_seconds: float = 30.0) -> List[Tuple[str, float]]:
    """Refina um take: mede cada clipe contra o primeiro que tenha audio.

    Devolve [(caminho, ajuste em segundos)]. Nao altera nada por conta propria —
    quem decide aplicar e a camada de cima.
    """
    with_audio = [c for c in clips if c.has_audio]
    if len(with_audio) < 2:
        return []
    reference = with_audio[0]
    out: List[Tuple[str, float]] = []
    for clip in with_audio[1:]:
        result = cross_correlate(reference.path, clip.path,
                                 window_seconds=window_seconds)
        if result is None:
            continue
        # Diferenca de inicio ja conhecida pelo metadado.
        known = 0.0
        if clip.start and reference.start:
            known = (clip.start - reference.start).total_seconds()
        out.append((clip.path, result.offset_seconds - known))
    return out
