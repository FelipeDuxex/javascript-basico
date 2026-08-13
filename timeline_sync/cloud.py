"""Deteccao de volume de nuvem/rede (Google Drive, Dropbox, OneDrive, SMB).

Importa por dois motivos:

1. Avisar que a leitura sera mais lenta e recomendar o modo manifesto.
2. Desligar o fallback de `mtime`. No Drive a data de modificacao reflete a
   sincronizacao, nao a gravacao — usar isso posicionaria o clipe num horario
   inventado e contaminaria a deteccao de blocos. Melhor marcar como
   "data desconhecida" e pedir resolucao manual.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from typing import List, Optional

CLOUD_PATTERNS = [
    (r"google\s*drive", "Google Drive"),
    (r"gdrive", "Google Drive"),
    (r"drivefs", "Google Drive"),
    (r"meu drive", "Google Drive"),
    (r"my drive", "Google Drive"),
    (r"drives compartilhados", "Google Drive (compartilhado)"),
    (r"shared drives", "Google Drive (compartilhado)"),
    (r"dropbox", "Dropbox"),
    (r"onedrive", "OneDrive"),
    (r"icloud", "iCloud Drive"),
    (r"pcloud", "pCloud"),
    (r"\bbox\b", "Box"),
]

NETWORK_FS = {
    "nfs", "nfs4", "cifs", "smbfs", "smb3", "fuse.rclone", "fuse.gdrive",
    "fuse.google-drive-ocamlfuse", "fuse.dropbox", "afpfs", "webdav",
    "fuse.drivefs", "fuse.sshfs", "davfs",
}


@dataclass
class VolumeInfo:
    path: str
    is_cloud: bool = False
    provider: str = ""
    fstype: str = ""
    reason: str = ""

    @property
    def warning(self) -> Optional[str]:
        if not self.is_cloud:
            return None
        return (
            f"A pasta parece estar em {self.provider or 'volume de nuvem/rede'} "
            f"({self.reason}). A leitura direta pode ser lenta e consumir banda. "
            "Recomendado: rodar o modo manifesto enquanto o material ainda esta "
            "no cartao/disco local, ou deixar o cache aquecer uma vez."
        )


def _fstype(path: str) -> str:
    """Descobre o sistema de arquivos do ponto de montagem (Linux/macOS)."""
    if sys.platform.startswith("win"):
        return ""
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="ignore") as fh:
            mounts = [line.split() for line in fh]
    except OSError:
        return ""
    best = ""
    best_len = -1
    real = os.path.realpath(path)
    for parts in mounts:
        if len(parts) < 3:
            continue
        mount, fstype = parts[1], parts[2]
        mount = mount.replace("\\040", " ")
        if real == mount or real.startswith(mount.rstrip("/") + "/"):
            if len(mount) > best_len:
                best_len = len(mount)
                best = fstype
    return best


def inspect(path: str) -> VolumeInfo:
    """Classifica o volume onde a pasta esta."""
    info = VolumeInfo(path=path)
    lowered = os.path.abspath(path).replace("\\", "/").lower()

    for pattern, provider in CLOUD_PATTERNS:
        if re.search(pattern, lowered):
            info.is_cloud = True
            info.provider = provider
            info.reason = f"caminho contem '{pattern}'"
            break

    fstype = _fstype(path)
    info.fstype = fstype
    if fstype in NETWORK_FS:
        info.is_cloud = True
        info.provider = info.provider or f"volume de rede ({fstype})"
        info.reason = info.reason or f"sistema de arquivos {fstype}"

    # No Windows, unidade virtual do Drive Desktop (G:, H:...) sem ser fixa.
    if sys.platform.startswith("win") and not info.is_cloud:
        drive = os.path.splitdrive(os.path.abspath(path))[0].upper()
        if drive and drive not in ("C:",):
            try:
                import ctypes
                kind = ctypes.windll.kernel32.GetDriveTypeW(f"{drive}\\")
                # 4 = DRIVE_REMOTE
                if kind == 4:
                    info.is_cloud = True
                    info.provider = "unidade de rede"
                    info.reason = f"unidade {drive} e remota"
            except Exception:
                pass
    return info


def inspect_many(paths: List[str]) -> VolumeInfo:
    """Se qualquer pasta apontada estiver na nuvem, o projeto e tratado como tal."""
    result = VolumeInfo(path=", ".join(paths))
    for path in paths:
        info = inspect(path)
        if info.is_cloud:
            result.is_cloud = True
            result.provider = info.provider
            result.reason = info.reason
            result.fstype = info.fstype
            return result
        result.fstype = result.fstype or info.fstype
    return result
