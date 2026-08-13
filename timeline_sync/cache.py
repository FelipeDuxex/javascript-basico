"""Cache persistente de metadados.

Chave = nome do arquivo + tamanho em bytes. Deliberadamente NAO usa `mtime`:
no Google Drive a data de modificacao reflete a sincronizacao, muda sozinha e
invalidaria o cache a esmo.

Com isso, reprocessar uma semana inteira de material varias vezes enquanto se
ajustam os blocos custa zero I/O de rede.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any, Dict, Optional

from .config import CACHE_FILE, ensure_app_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS metadados (
    chave      TEXT PRIMARY KEY,
    nome       TEXT NOT NULL,
    tamanho    INTEGER NOT NULL,
    payload    TEXT NOT NULL,
    lido_em    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nome ON metadados(nome);
"""


def cache_key(filename: str, size: int) -> str:
    return f"{filename.lower()}|{size}"


class MetadataCache:
    """SQLite com uma tabela. Seguro para uso concorrente (lock proprio)."""

    def __init__(self, path: Optional[str] = None):
        ensure_app_dir()
        self.path = path or CACHE_FILE
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self.hits = 0
        self.misses = 0

    def get(self, filename: str, size: int) -> Optional[Dict[str, Any]]:
        key = cache_key(filename, size)
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM metadados WHERE chave = ?", (key,)
            ).fetchone()
        if row is None:
            self.misses += 1
            return None
        try:
            data = json.loads(row[0])
        except json.JSONDecodeError:
            self.misses += 1
            return None
        self.hits += 1
        return data

    def put(self, filename: str, size: int, payload: Dict[str, Any]) -> None:
        import time
        key = cache_key(filename, size)
        blob = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO metadados (chave, nome, tamanho, payload, lido_em)"
                " VALUES (?, ?, ?, ?, ?)",
                (key, filename, size, blob, time.time()),
            )
            self._conn.commit()

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM metadados").fetchone()[0]

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM metadados")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
