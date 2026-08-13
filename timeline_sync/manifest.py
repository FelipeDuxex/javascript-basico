"""Modo manifesto de ingestao — a solucao preventiva para o Google Drive.

Fluxo recomendado:

  1. Cartao ainda na maquina (ou material em disco local):
         python -m timeline_sync manifesto /caminho/do/cartao
     Isso e rapido: e disco fisico. Gera `manifesto.json` na pasta.

  2. Sobe a pasta para o Drive com o `manifesto.json` dentro.

  3. Nas proximas vezes, o app detecta o manifesto e le **so ele**.
     Nenhum byte de video e transferido. Processamento vira instantaneo.

O manifesto e indexado por nome de arquivo + tamanho em bytes — nunca por
`mtime`, que muda na sincronizacao do Drive.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

from . import __version__
from .config import MANIFEST_NAME

MANIFEST_VERSION = 1


def manifest_path(folder: str) -> str:
    return os.path.join(folder, MANIFEST_NAME)


def write_manifest(folder: str, records: Dict[str, Dict[str, Any]],
                   extra: Optional[Dict[str, Any]] = None) -> str:
    """Escreve `manifesto.json` na pasta. `records` indexado por nome|tamanho."""
    payload = {
        "manifest_version": MANIFEST_VERSION,
        "gerado_por": f"timeline_sync {__version__}",
        "gerado_em": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "pasta_origem": os.path.abspath(folder),
        "total": len(records),
        "arquivos": records,
    }
    if extra:
        payload.update(extra)
    dest = manifest_path(folder)
    tmp = dest + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, dest)
    return dest


def read_manifest(path: str) -> Dict[str, Dict[str, Any]]:
    """Le um `manifesto.json` e devolve o dicionario de registros."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    arquivos = data.get("arquivos")
    if not isinstance(arquivos, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for key, value in arquivos.items():
        if isinstance(value, dict):
            out[str(key).lower()] = value
    return out


def find_manifests(folders: List[str]) -> List[str]:
    """Procura `manifesto.json` nas pastas apontadas e em suas subpastas."""
    found: List[str] = []
    for folder in folders:
        folder = os.path.expanduser(folder)
        if os.path.isfile(folder):
            folder = os.path.dirname(folder)
        if not os.path.isdir(folder):
            continue
        direct = manifest_path(folder)
        if os.path.isfile(direct):
            found.append(direct)
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            if MANIFEST_NAME in files:
                full = os.path.join(root, MANIFEST_NAME)
                if full not in found:
                    found.append(full)
    return found


def load_manifests(folders: List[str]) -> Dict[str, Dict[str, Any]]:
    """Junta todos os manifestos encontrados num unico indice."""
    merged: Dict[str, Dict[str, Any]] = {}
    for path in find_manifests(folders):
        merged.update(read_manifest(path))
    return merged


def build_manifest(folder: str, config=None, progress=None) -> Dict[str, Any]:
    """Varre a pasta local e gera o manifesto. Deve rodar em disco fisico.

    Retorna um resumo com contagens e o caminho gerado.
    """
    from .cache import MetadataCache
    from .config import Config
    from .reader import find_media, read_record

    cfg = config or Config()
    files = find_media([folder], recursive=cfg.recursive)
    cache = MetadataCache()
    records: Dict[str, Dict[str, Any]] = {}
    bytes_read = 0
    sem_data: List[str] = []

    for index, (path, size) in enumerate(files, start=1):
        record = read_record(path, size, config=cfg, cache=cache, manifest=None,
                             allow_mtime=True)
        # Guardamos o caminho relativo para conferencia humana; a chave de
        # busca continua sendo nome|tamanho.
        record["caminho_relativo"] = os.path.relpath(path, folder)
        bytes_read += int(record.get("bytes_read") or 0)
        records[f"{os.path.basename(path).lower()}|{size}"] = record
        if not record.get("raw_time"):
            sem_data.append(os.path.basename(path))
        if progress is not None:
            progress({"done": index, "total": len(files),
                      "current": os.path.basename(path)})

    dest = write_manifest(folder, records, extra={"bytes_lidos": bytes_read})
    cache.close()
    return {
        "manifesto": dest,
        "arquivos": len(records),
        "bytes_lidos": bytes_read,
        "bytes_totais": sum(s for _, s in files),
        "sem_data": sem_data,
    }
