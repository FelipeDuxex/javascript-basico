"""Identificacao de dispositivo e correcao de relogio.

O problema central: a Sony a7III foi comprada nos EUA. Cameras Sony escrevem no
`creation_time` o valor **literal do relogio interno** e rotulam como UTC
("...Z") sem realmente converter fuso. Se o relogio nunca foi ajustado, o
horario pode estar errado por horas inteiras e o sufixo Z e enganoso.

O iPhone, ao contrario, acerta a hora sozinho via rede/GPS e grava
`com.apple.quicktime.creationdate` com offset de fuso explicito. Por isso o
iPhone e SEMPRE a referencia: para cadastrar qualquer camera, basta um clipe
dela e um clipe do iPhone gravados no mesmo momento.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .config import DEVICES_FILE, ensure_app_dir
from .models import (TRUST_HIGH, TRUST_LOW, TRUST_MEDIUM, TRUST_NONE, Clip)
from .timeutil import format_offset, to_local_naive

VIDEO_EXT = {".mp4", ".mov", ".mxf", ".m4v", ".avi", ".mts", ".m2ts", ".insv"}
AUDIO_EXT = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".aif", ".aiff", ".ogg"}
MEDIA_EXT = VIDEO_EXT | AUDIO_EXT

# --- padroes de nome de arquivo (fallback quando nao ha metadado) ----------
NAME_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"^C\d{3,4}", re.I), "sony"),          # C0020.MP4
    (re.compile(r"^DJI[_-]", re.I), "dji"),            # DJI_0001.MP4
    (re.compile(r"^IMG[_-]", re.I), "iphone"),         # IMG_1234.MOV
    (re.compile(r"^MVI[_-]", re.I), "canon"),
    (re.compile(r"^(HL|LARK|HOLLY)", re.I), "hollyland"),
    (re.compile(r"^ZOOM", re.I), "gravador"),
    (re.compile(r"^(TASCAM|DR\d)", re.I), "gravador"),
]

MAKE_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"sony", re.I), "sony"),
    (re.compile(r"apple", re.I), "iphone"),
    (re.compile(r"\bdji\b|sz\s*dji|da[- ]?jiang", re.I), "dji"),
    (re.compile(r"hollyland", re.I), "hollyland"),
    (re.compile(r"canon", re.I), "canon"),
    (re.compile(r"nikon", re.I), "nikon"),
    (re.compile(r"panasonic|lumix", re.I), "panasonic"),
    (re.compile(r"gopro", re.I), "gopro"),
    (re.compile(r"blackmagic", re.I), "blackmagic"),
]

MODEL_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"iphone|ipad", re.I), "iphone"),
    (re.compile(r"ilce|dsc-|fdr-|fx\d|a7|a6\d00", re.I), "sony"),
    (re.compile(r"osmo|mavic|air \d|mini \d|pocket|ronin|avata", re.I), "dji"),
    (re.compile(r"lark|mars|solidcom", re.I), "hollyland"),
]

FRIENDLY_KIND = {
    "sony": "Sony",
    "iphone": "iPhone",
    "dji": "DJI",
    "hollyland": "Hollyland",
    "canon": "Canon",
    "nikon": "Nikon",
    "panasonic": "Panasonic",
    "gopro": "GoPro",
    "blackmagic": "Blackmagic",
    "gravador": "Gravador de audio",
    "desconhecido": "Desconhecido",
}

# Ordem das trilhas: video primeiro (V1..Vn), audio externo por ultimo.
KIND_TRACK_ORDER = ["sony", "iphone", "dji", "canon", "nikon", "panasonic",
                    "gopro", "blackmagic", "desconhecido", "gravador",
                    "hollyland"]


@dataclass
class DeviceProfile:
    """Perfil persistente de um dispositivo."""

    key: str
    kind: str = "desconhecido"
    label: str = ""
    make: str = ""
    model: str = ""
    # Offset calibrado contra o iPhone, em segundos (somado ao horario lido).
    offset_seconds: float = 0.0
    # Ajuste fino manual, somado ao offset calibrado. Valvula de escape.
    manual_offset_seconds: float = 0.0
    calibrated_at: str = ""
    calibration_note: str = ""
    # Historico de calibracoes (relogio de camera zera quando a bateria
    # interna descarrega, e util saber quando foi a ultima).
    history: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def total_offset(self) -> float:
        return self.offset_seconds + self.manual_offset_seconds

    @property
    def calibrated(self) -> bool:
        return bool(self.calibrated_at)

    def display(self) -> str:
        base = f"{self.label or self.key} → offset: {format_offset(self.total_offset)}"
        if self.calibrated_at:
            try:
                when = datetime.fromisoformat(self.calibrated_at)
                base += f" | calibrado em {when.strftime('%d/%m/%Y')}"
            except ValueError:
                base += f" | calibrado em {self.calibrated_at}"
        else:
            base += " | NAO CALIBRADO"
        return base

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class DeviceRegistry:
    """Perfis salvos em `dispositivos.json`."""

    def __init__(self, path: Optional[str] = None):
        ensure_app_dir()
        self.path = path or DEVICES_FILE
        self.profiles: Dict[str, DeviceProfile] = {}
        self.load()

    # -- persistencia ---------------------------------------------------
    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return
        for key, raw in (data.get("devices") or {}).items():
            raw = dict(raw)
            raw["key"] = key
            allowed = set(DeviceProfile.__dataclass_fields__)
            self.profiles[key] = DeviceProfile(
                **{k: v for k, v in raw.items() if k in allowed}
            )

    def save(self) -> str:
        payload = {
            "version": 1,
            "devices": {k: v.to_dict() for k, v in self.profiles.items()},
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)
        return self.path

    # -- consulta / criacao ---------------------------------------------
    def get(self, key: str) -> Optional[DeviceProfile]:
        return self.profiles.get(key)

    def ensure(self, key: str, kind: str, make: str = "", model: str = "") -> DeviceProfile:
        prof = self.profiles.get(key)
        if prof is None:
            prof = DeviceProfile(
                key=key, kind=kind, make=make, model=model,
                label=default_label(kind, model, key),
            )
            self.profiles[key] = prof
        else:
            prof.make = prof.make or make
            prof.model = prof.model or model
            if not prof.label:
                prof.label = default_label(kind, model, key)
        return prof

    def rename(self, key: str, label: str) -> bool:
        prof = self.profiles.get(key)
        if prof is None:
            return False
        prof.label = label.strip() or prof.label
        self.save()
        return True

    def set_manual_offset(self, key: str, seconds: float) -> bool:
        prof = self.profiles.get(key)
        if prof is None:
            return False
        prof.manual_offset_seconds = float(seconds)
        self.save()
        return True

    def delete(self, key: str) -> bool:
        if key in self.profiles:
            del self.profiles[key]
            self.save()
            return True
        return False

    def apply_calibration(self, key: str, offset_seconds: float, note: str,
                          kind: str = "desconhecido", make: str = "",
                          model: str = "") -> DeviceProfile:
        prof = self.ensure(key, kind, make, model)
        if prof.calibrated_at:
            prof.history.append({
                "offset_seconds": prof.offset_seconds,
                "calibrated_at": prof.calibrated_at,
                "note": prof.calibration_note,
            })
        prof.offset_seconds = float(offset_seconds)
        prof.calibrated_at = datetime.now().replace(microsecond=0).isoformat()
        prof.calibration_note = note
        self.save()
        return prof


def default_label(kind: str, model: str, key: str) -> str:
    friendly = FRIENDLY_KIND.get(kind, kind.title())
    if model and model.lower() not in friendly.lower():
        return f"{friendly} {model}".strip()
    return friendly or key


# ----------------------------------------------------------------------
# identificacao
# ----------------------------------------------------------------------

def identify(filename: str, make: str, model: str, has_video: bool,
             has_audio: bool) -> Tuple[str, str]:
    """Descobre (kind, chave canonica) de um arquivo.

    Prioridade: fabricante -> modelo -> padrao de nome. Um arquivo somente de
    audio sem metadado e tratado como gravador externo (Hollyland no meu set).
    """
    kind = ""
    for pattern, candidate in MAKE_PATTERNS:
        if make and pattern.search(make):
            kind = candidate
            break
    if not kind:
        for pattern, candidate in MODEL_PATTERNS:
            if model and pattern.search(model):
                kind = candidate
                break
    if not kind:
        base = os.path.basename(filename)
        for pattern, candidate in NAME_PATTERNS:
            if pattern.search(base):
                kind = candidate
                break
    if not kind and has_audio and not has_video:
        kind = "hollyland"
    if not kind:
        kind = "desconhecido"

    ident = (model or make or "").strip()
    if not ident:
        ident = "sem-modelo"
    key = f"{kind}:{_slug(ident)}"
    return kind, key


def _slug(text: str) -> str:
    out = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")
    return out or "sem-modelo"


def track_index_for(kind: str, key: str, assigned: Dict[str, int]) -> int:
    """Indice de trilha estavel por dispositivo (posicao vertical = aparelho)."""
    if key in assigned:
        return assigned[key]
    rank = KIND_TRACK_ORDER.index(kind) if kind in KIND_TRACK_ORDER else 50
    assigned[key] = rank * 100 + len([k for k in assigned if k.startswith(kind + ":")])
    return assigned[key]


# ----------------------------------------------------------------------
# aplicacao de offset
# ----------------------------------------------------------------------

def apply_clock_correction(clip: Clip, profile: Optional[DeviceProfile],
                           tzname: str) -> None:
    """Define `clip.start` (hora de parede local) e o nivel de confianca.

    Duas situacoes bem diferentes:

    * `tz_known=True` (iPhone, sidecar com fuso): o instante e absoluto e
      confiavel. Convertemos para o fuso do projeto. Nenhum offset e aplicado.
    * `tz_known=False` (Sony/DJI, `mvhd` rotulado Z): o valor lido e o relogio
      literal da camera. Tratamos como hora local e somamos o offset calibrado.
    """
    if clip.raw_time is None:
        clip.start = None
        clip.trust = TRUST_NONE
        return

    if clip.tz_known:
        clip.start = to_local_naive(clip.raw_time, tzname)
        clip.offset_seconds = 0.0
        clip.trust = TRUST_HIGH
        if profile is not None and abs(profile.manual_offset_seconds) > 0:
            # Ajuste fino manual vale mesmo para fonte confiavel: o usuario
            # manda mais que a heuristica.
            from datetime import timedelta
            clip.offset_seconds = profile.manual_offset_seconds
            clip.start = clip.start + timedelta(seconds=profile.manual_offset_seconds)
        return

    from datetime import timedelta
    literal = clip.raw_time.replace(tzinfo=None)
    offset = profile.total_offset if profile else 0.0
    clip.offset_seconds = offset
    clip.start = literal + timedelta(seconds=offset)
    if profile is not None and profile.calibrated:
        clip.trust = TRUST_MEDIUM
    else:
        clip.trust = TRUST_LOW
        clip.warnings.append(
            "dispositivo sem calibracao — o horario pode estar errado por horas"
        )


# ----------------------------------------------------------------------
# calibracao contra o iPhone
# ----------------------------------------------------------------------

@dataclass
class CalibrationResult:
    ok: bool = False
    message: str = ""
    device_key: str = ""
    device_kind: str = ""
    device_model: str = ""
    offset_seconds: float = 0.0
    reference_time: str = ""
    device_time: str = ""
    refined_by_audio: bool = False
    audio_shift_seconds: float = 0.0
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["offset_human"] = format_offset(self.offset_seconds)
        return data


def calibrate(device_file: str, reference_file: str, tzname: str,
              refine_audio: bool = True,
              registry: Optional[DeviceRegistry] = None) -> CalibrationResult:
    """Calcula o offset de uma camera usando um clipe do iPhone como verdade.

    Nao exige que as duas gravacoes tenham comecado no mesmo instante exato:
    poucos segundos de diferenca sao irrelevantes frente a um erro de horas.
    Se os dois clipes tiverem audio, o pico sonoro (palma/claquete) e usado
    para zerar tambem o erro de segundos.
    """
    from .reader import read_one   # import tardio: evita ciclo

    res = CalibrationResult()
    dev = read_one(device_file, use_ffprobe=True, use_mtime=False)
    ref = read_one(reference_file, use_ffprobe=True, use_mtime=False)

    if dev.raw_time is None:
        res.message = f"Nao consegui ler a hora de gravacao de {os.path.basename(device_file)}."
        return res
    if ref.raw_time is None:
        res.message = f"Nao consegui ler a hora de gravacao de {os.path.basename(reference_file)}."
        return res

    ref_local = to_local_naive(ref.raw_time, tzname)
    dev_literal = dev.raw_time.replace(tzinfo=None)

    if not ref.tz_known:
        res.note = (
            "ATENCAO: o clipe de referencia nao trouxe fuso explicito. "
            "Confirme que ele e realmente do iPhone — a referencia precisa ser "
            "um aparelho que acerta a hora sozinho."
        )

    offset = (ref_local - dev_literal).total_seconds()

    if refine_audio and dev.has_audio and ref.has_audio:
        from .audiosync import peak_offset
        dev_peak = peak_offset(device_file)
        ref_peak = peak_offset(reference_file)
        if dev_peak is not None and ref_peak is not None:
            shift = ref_peak - dev_peak
            # So aceitamos o refino se ele for um ajuste fino de verdade.
            if abs(shift) <= 30.0:
                offset += shift
                res.refined_by_audio = True
                res.audio_shift_seconds = shift

    res.ok = True
    res.device_key = dev.device_key
    res.device_kind = dev.device_kind
    res.device_model = dev.model
    res.offset_seconds = offset
    res.reference_time = ref_local.isoformat() if ref_local else ""
    res.device_time = dev_literal.isoformat()
    res.message = (
        f"{dev.device_label}: relogio marcava {dev_literal.strftime('%d/%m %H:%M:%S')}, "
        f"hora real {ref_local.strftime('%d/%m %H:%M:%S')} → "
        f"offset {format_offset(offset)}"
    )
    if res.refined_by_audio:
        res.message += f" (refinado por audio em {res.audio_shift_seconds:+.2f}s)"

    if registry is not None:
        note = (
            f"calibrado com {os.path.basename(device_file)} vs "
            f"{os.path.basename(reference_file)}"
            + (" + refino por audio" if res.refined_by_audio else "")
        )
        registry.apply_calibration(
            res.device_key, offset, note, kind=dev.device_kind,
            make=dev.make, model=dev.model,
        )
    return res
