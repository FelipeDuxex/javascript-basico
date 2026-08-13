"""Interface local: servidor HTTP da biblioteca padrao + uma pagina.

Escolhi `http.server` em vez de Flask/FastAPI de proposito: o app roda so na
minha maquina, para mim, e assim nao existe nenhuma dependencia para instalar —
`python -m timeline_sync web` e suficiente. Ver DECISOES.md.

Nada sai da maquina: o servidor escuta apenas em 127.0.0.1.
"""

from __future__ import annotations

import json
import mimetypes
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from .. import APP_NAME, __version__
from ..config import ProjectState, list_projects
from ..devices import calibrate
from ..pipeline import Session
from ..runtime import is_frozen, resource_path

# No executavel congelado os estaticos ficam na pasta temporaria do PyInstaller,
# nao ao lado deste modulo.
STATIC_DIR = (resource_path("timeline_sync", "web", "static") if is_frozen()
              else os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"))


class _State:
    session: Optional[Session] = None
    lock = threading.Lock()


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = f"TimelineSync/{__version__}"

    # -- infra ----------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:  # silencia o log padrao
        if os.environ.get("TIMELINE_SYNC_DEBUG"):
            super().log_message(fmt, *args)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _send_json(self, payload: Any, code: int = 200) -> None:
        self._send(code, _json_bytes(payload), "application/json; charset=utf-8")

    def _read_json(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _static(self, rel: str) -> None:
        rel = rel.lstrip("/")
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
            self._send(404, b"nao encontrado", "text/plain; charset=utf-8")
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as fh:
            self._send(200, fh.read(), f"{ctype}; charset=utf-8"
                       if ctype.startswith("text") or "javascript" in ctype else ctype)

    # -- GET ------------------------------------------------------------
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        session = _State.session

        if path in ("/", "/index.html"):
            self._static("index.html")
            return
        if path.startswith("/static/"):
            self._static(path[len("/static/"):])
            return
        if path == "/api/estado":
            self._send_json(session.snapshot() if session else {})
            return
        if path == "/api/progresso":
            self._send_json(session.progress.to_dict() if session else {})
            return
        if path == "/api/timeline":
            day = (query.get("dia") or [""])[0]
            self._send_json(session.timeline_payload(day) if session else {})
            return
        if path == "/api/relatorio":
            if session is None or session.project is None:
                self._send(200, b"", "text/plain; charset=utf-8")
                return
            from ..report import build_report
            text = build_report(session.project, session.config,
                                scan_seconds=session.project.read_seconds)
            self._send(200, text.encode("utf-8"), "text/plain; charset=utf-8")
            return
        if path == "/api/projetos":
            self._send_json({"projetos": list_projects()})
            return
        self._send(404, b"nao encontrado", "text/plain; charset=utf-8")

    # -- POST -----------------------------------------------------------
    def do_POST(self) -> None:
        path = urlparse(self.path).path
        body = self._read_json()
        session = _State.session
        if session is None:
            self._send_json({"erro": "sessao nao inicializada"}, 500)
            return

        try:
            handler = {
                "/api/varrer": self._varrer,
                "/api/cancelar": self._cancelar,
                "/api/config": self._config,
                "/api/limiar": self._limiar,
                "/api/bloco/mesclar": self._mesclar,
                "/api/bloco/dividir": self._dividir,
                "/api/dia/resetar": self._resetar,
                "/api/dispositivo": self._dispositivo,
                "/api/calibrar": self._calibrar,
                "/api/exportar": self._exportar,
                "/api/manifesto": self._manifesto,
                "/api/refinar-audio": self._refinar,
                "/api/projeto": self._projeto,
                "/api/cache/limpar": self._limpar_cache,
            }.get(path)
            if handler is None:
                self._send(404, b"nao encontrado", "text/plain; charset=utf-8")
                return
            self._send_json(handler(session, body))
        except Exception as exc:
            self._send_json({"erro": f"{type(exc).__name__}: {exc}"}, 500)

    # -- acoes ----------------------------------------------------------
    def _varrer(self, session: Session, body: Dict[str, Any]) -> Dict[str, Any]:
        folders = [f for f in (body.get("pastas") or []) if str(f).strip()]
        if not folders:
            return {"erro": "informe pelo menos uma pasta"}
        started = session.scan_async(folders)
        return {"ok": started, "erro": "" if started else "ja existe uma varredura rodando"}

    def _cancelar(self, session: Session, body: Dict[str, Any]) -> Dict[str, Any]:
        session.cancel()
        return {"ok": True}

    def _config(self, session: Session, body: Dict[str, Any]) -> Dict[str, Any]:
        session.update_config(body)
        return session.snapshot()

    def _limiar(self, session: Session, body: Dict[str, Any]) -> Dict[str, Any]:
        seconds = body.get("segundos")
        seconds = None if seconds in (None, "", "auto") else float(seconds)
        if body.get("todos"):
            session.set_global_threshold(seconds)
        else:
            session.set_day_threshold(str(body.get("dia") or ""), seconds)
        return session.snapshot()

    def _mesclar(self, session: Session, body: Dict[str, Any]) -> Dict[str, Any]:
        session.merge_block(str(body.get("dia") or ""), int(body.get("bloco") or 0))
        return session.snapshot()

    def _dividir(self, session: Session, body: Dict[str, Any]) -> Dict[str, Any]:
        session.split_block(str(body.get("dia") or ""), str(body.get("em") or ""))
        return session.snapshot()

    def _resetar(self, session: Session, body: Dict[str, Any]) -> Dict[str, Any]:
        session.reset_day(str(body.get("dia") or ""))
        return session.snapshot()

    def _dispositivo(self, session: Session, body: Dict[str, Any]) -> Dict[str, Any]:
        key = str(body.get("chave") or "")
        if body.get("apagar"):
            session.registry.delete(key)
        else:
            if body.get("apelido") is not None:
                session.registry.rename(key, str(body["apelido"]))
            if body.get("offset_manual") is not None:
                session.registry.set_manual_offset(key, float(body["offset_manual"]))
        session.rebuild()
        return session.snapshot()

    def _calibrar(self, session: Session, body: Dict[str, Any]) -> Dict[str, Any]:
        device_file = str(body.get("arquivo_camera") or "")
        reference = str(body.get("arquivo_iphone") or "")
        if not os.path.isfile(device_file) or not os.path.isfile(reference):
            return {"erro": "informe os dois arquivos (camera e iPhone)"}
        result = calibrate(device_file, reference, session.config.timezone,
                           refine_audio=bool(body.get("refinar_audio", True)),
                           registry=session.registry)
        session.rebuild()
        payload = session.snapshot()
        payload["calibracao"] = result.to_dict()
        return payload

    def _exportar(self, session: Session, body: Dict[str, Any]) -> Dict[str, Any]:
        out_dir = str(body.get("saida") or "saida")
        days = body.get("dias") or None
        result = session.export(
            out_dir,
            day_keys=days,
            include_master=bool(body.get("master", session.config.export_master)),
            one_file_per_day=not bool(body.get("arquivo_unico")),
        )
        return {"exportacao": result}

    def _manifesto(self, session: Session, body: Dict[str, Any]) -> Dict[str, Any]:
        from ..manifest import build_manifest
        folder = str(body.get("pasta") or "")
        if not os.path.isdir(folder):
            return {"erro": "pasta invalida"}
        return {"manifesto": build_manifest(folder, config=session.config)}

    def _refinar(self, session: Session, body: Dict[str, Any]) -> Dict[str, Any]:
        from ..audiosync import available, refine_take
        if not available():
            return {"erro": "ffmpeg nao encontrado — refino por audio indisponivel"}
        day_key = str(body.get("dia") or "")
        day = next((d for d in (session.project.days if session.project else [])
                    if d.key == day_key), None)
        if day is None:
            return {"erro": "dia nao encontrado"}
        out = []
        for block in day.blocks:
            for take in block.takes:
                for path, shift in refine_take(take.clips):
                    out.append({"arquivo": os.path.basename(path),
                                "ajuste_segundos": round(shift, 3),
                                "take": take.index, "bloco": block.index})
        return {"refinamentos": out}

    def _projeto(self, session: Session, body: Dict[str, Any]) -> Dict[str, Any]:
        name = str(body.get("nome") or "").strip()
        if not name:
            return {"erro": "informe o nome do projeto"}
        state = ProjectState.load(name)
        state.name = name
        new_session = Session(state=state, registry=session.registry,
                              cache=session.cache)
        _State.session = new_session
        return new_session.snapshot()

    def _limpar_cache(self, session: Session, body: Dict[str, Any]) -> Dict[str, Any]:
        session.cache.clear()
        return {"ok": True, "cache_count": session.cache.count()}


def serve(port: int = 8730, project: str = "projeto",
          folders: Optional[list] = None, open_browser: bool = True,
          host: str = "127.0.0.1") -> int:
    state = ProjectState.load(project)
    state.name = project
    if folders:
        state.folders = [os.path.abspath(os.path.expanduser(f)) for f in folders]
    session = Session(state=state)
    _State.session = session

    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print(f"{APP_NAME} {__version__}")
    print(f"interface local em {url}")
    print(f"projeto: {project} | pastas salvas: {len(state.folders)}")
    print("Ctrl+C para encerrar.")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nencerrando...")
    finally:
        httpd.server_close()
        session.close()
    return 0
