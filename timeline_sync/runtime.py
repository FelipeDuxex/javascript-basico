"""Diferencas entre rodar do codigo-fonte e rodar do executavel congelado.

Duas coisas mudam quando o app vira um `.exe` empacotado com PyInstaller:

1. **Onde estao os arquivos estaticos.** No modo onefile o executavel se
   descompacta numa pasta temporaria apontada por `sys._MEIPASS`. Resolver
   caminho por `__file__` funciona por acidente e quebra em algumas versoes —
   melhor ser explicito.

2. **Onde estao `ffmpeg`/`ffprobe`.** Eles nao vao dentro do executavel (sao
   dezenas de MB e tem licenca propria). O app funciona sem eles, porque os
   parsers proprios cobrem MP4/MOV/WAV. Mas se voce largar `ffmpeg.exe` e
   `ffprobe.exe` **na mesma pasta do TimelineSync.exe**, o app acha sozinho e o
   refino por audio passa a funcionar — sem mexer no PATH do Windows.
"""

from __future__ import annotations

import os
import shutil
import sys
from typing import List, Optional


def configurar_console() -> None:
    """Garante que o console aguenta o texto que o app imprime.

    O console do Windows usa cp1252 por padrao, e cp1252 nao tem `→` — que
    aparece em todo lugar aqui ("relogio marcava X → hora real Y", as notas de
    deteccao de bloco, o display dos perfis). O resultado era um
    `UnicodeEncodeError` que derrubava `dispositivos`, `organizar` e `calibrar`
    no meio da execucao, tanto no executavel quanto rodando do codigo-fonte.

    Duas medidas, porque uma so nao basta:

    * `SetConsoleOutputCP(65001)` poe o console em UTF-8, para o texto *aparecer*
      certo;
    * `reconfigure(errors="replace")` garante que, se ainda assim algum caractere
      nao couber na saida (redirecionamento para arquivo, terminal antigo), ele
      vire `?` em vez de abortar o comando. Perder um simbolo e aceitavel;
      perder a execucao inteira, nao.
    """
    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def is_frozen() -> bool:
    """True quando rodando de dentro de um executavel PyInstaller."""
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def resource_path(*parts: str) -> str:
    """Caminho de um recurso empacotado (ex.: os arquivos da interface)."""
    if is_frozen():
        base = sys._MEIPASS  # type: ignore[attr-defined]
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


def app_dir() -> str:
    """Pasta onde o executavel esta (nao a temporaria de descompactacao).

    E aqui que procuramos binarios auxiliares largados pelo usuario.
    """
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _candidates(name: str) -> List[str]:
    exts = [".exe", ".EXE"] if sys.platform.startswith("win") else [""]
    folders = [app_dir(), os.path.join(app_dir(), "ferramentas"),
               os.path.join(app_dir(), "bin"), os.path.join(app_dir(), "ffmpeg"),
               os.path.join(app_dir(), "ffmpeg", "bin")]
    out = []
    for folder in folders:
        for ext in exts:
            out.append(os.path.join(folder, name + ext))
    return out


def find_tool(name: str) -> Optional[str]:
    """Localiza um binario auxiliar: primeiro ao lado do app, depois no PATH."""
    for candidate in _candidates(name):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    found = shutil.which(name)
    return found


def tool_status() -> dict:
    """Resumo do que esta disponivel, para exibir na interface e no console."""
    ffmpeg = find_tool("ffmpeg")
    ffprobe = find_tool("ffprobe")
    return {
        "ffmpeg": ffmpeg or "",
        "ffprobe": ffprobe or "",
        "tem_ffmpeg": bool(ffmpeg),
        "tem_ffprobe": bool(ffprobe),
        "congelado": is_frozen(),
        "pasta_app": app_dir(),
    }
