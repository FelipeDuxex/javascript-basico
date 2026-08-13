"""Linha de comando do Timeline Sync."""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import List, Optional, Sequence

from . import __version__
from .cache import MetadataCache
from .config import Config, ProjectState, list_projects
from .devices import DeviceRegistry, calibrate
from .pipeline import Session
from .report import build_report
from .timeutil import format_bytes, format_offset


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="timeline_sync",
        description="Organiza material multi-camera por horario real e exporta "
                    "timelines para o Premiere Pro. Uso pessoal, 100%% local.",
    )
    p.add_argument("--versao", action="version", version=f"timeline_sync {__version__}")
    sub = p.add_subparsers(dest="comando", required=True)

    # organizar
    org = sub.add_parser("organizar", help="varre pastas, organiza e exporta XMLs")
    org.add_argument("pastas", nargs="+", help="pastas com o material bruto")
    org.add_argument("--saida", "-o", default="saida", help="pasta dos XMLs e do relatorio")
    org.add_argument("--projeto", "-p", default="projeto", help="nome do projeto (estado salvo)")
    org.add_argument("--dias", help="exportar so estes dias (ex: 2026-08-13,2026-08-15)")
    org.add_argument("--sem-master", action="store_true", help="nao gerar a sequencia MASTER")
    org.add_argument("--sem-exportar", action="store_true", help="so analisar, sem gerar XML")
    org.add_argument("--arquivo-unico", action="store_true",
                     help="todas as sequencias num unico XML")
    org.add_argument("--json", action="store_true", help="imprime o resultado em JSON")
    _add_config_args(org)

    # manifesto
    man = sub.add_parser("manifesto",
                         help="gera manifesto.json (rodar no cartao/disco local)")
    man.add_argument("pasta", help="pasta local com o material")
    _add_config_args(man)

    # calibrar
    cal = sub.add_parser("calibrar",
                         help="cadastra o relogio de uma camera usando o iPhone como referencia")
    cal.add_argument("arquivo_camera", help="clipe da camera a cadastrar")
    cal.add_argument("arquivo_iphone", help="clipe do iPhone gravado no mesmo momento")
    cal.add_argument("--sem-audio", action="store_true",
                     help="nao tentar refinar pelo pico de audio")
    cal.add_argument("--fuso", default="America/Sao_Paulo")

    # dispositivos
    dev = sub.add_parser("dispositivos", help="ver/editar/apagar perfis salvos")
    dev.add_argument("--renomear", metavar="CHAVE=APELIDO", action="append", default=[])
    dev.add_argument("--offset", metavar="CHAVE=SEGUNDOS", action="append", default=[],
                     help="ajuste fino manual, somado ao offset calibrado")
    dev.add_argument("--apagar", metavar="CHAVE", action="append", default=[])

    # web
    web = sub.add_parser("web", help="abre a interface local no navegador")
    web.add_argument("--porta", type=int, default=8730)
    web.add_argument("--projeto", "-p", default="projeto")
    web.add_argument("--pastas", nargs="*", default=[], help="ja aponta estas pastas")
    web.add_argument("--sem-navegador", action="store_true")

    # cache
    cache = sub.add_parser("cache", help="estado do cache de metadados")
    cache.add_argument("--limpar", action="store_true")

    # projetos
    sub.add_parser("projetos", help="lista os projetos salvos")

    return p


def _add_config_args(parser: argparse.ArgumentParser) -> None:
    g = parser.add_argument_group("parametros de organizacao")
    g.add_argument("--fuso", help="fuso do projeto (padrao America/Sao_Paulo)")
    g.add_argument("--virada-dia", type=float, metavar="HORA",
                   help="hora da virada de dia (padrao 4.0)")
    g.add_argument("--metodo-bloco", choices=["auto", "ratio", "jenks", "kmeans", "fixo"])
    g.add_argument("--piso-pausa", type=float, metavar="SEG",
                   help="intervalo minimo que pode quebrar bloco (padrao 120)")
    g.add_argument("--teto-blocos", type=int, metavar="N",
                   help="maximo de blocos por dia (padrao 6)")
    g.add_argument("--limiar", type=float, metavar="SEG",
                   help="limiar manual de pausa aplicado a todos os dias")
    g.add_argument("--janela-take", type=float, metavar="SEG",
                   help="janela de agrupamento de take (padrao 10)")
    g.add_argument("--tempo-morto", choices=["preserve", "compress", "close"])
    g.add_argument("--cores", choices=["bloco", "dia", "dispositivo"])
    g.add_argument("--workers", type=int)
    g.add_argument("--raiz-midia", help="reescreve esta raiz nos caminhos do XML")
    g.add_argument("--nova-raiz-midia", help="raiz de destino para os caminhos do XML")
    g.add_argument("--sem-ffprobe", action="store_true",
                   help="nao usar ffprobe como fallback")
    g.add_argument("--timebase", type=int, help="frame rate da sequencia (padrao 30)")
    g.add_argument("--resolucao", help="ex: 1080x1920")


def _config_from_args(args, base: Optional[Config] = None) -> Config:
    cfg = base or Config()
    mapping = {
        "fuso": "timezone",
        "virada_dia": "day_start_hour",
        "metodo_bloco": "block_method",
        "piso_pausa": "block_min_gap_seconds",
        "teto_blocos": "block_max_per_day",
        "limiar": "block_manual_threshold_all_days",
        "janela_take": "take_window_seconds",
        "tempo_morto": "gap_mode",
        "cores": "color_mode",
        "workers": "workers",
        "raiz_midia": "media_root_original",
        "nova_raiz_midia": "media_root_override",
        "timebase": "sequence_timebase",
    }
    updates = {}
    for arg, field in mapping.items():
        value = getattr(args, arg, None)
        if value is not None:
            updates[field] = value
    if getattr(args, "sem_ffprobe", False):
        updates["use_ffprobe_fallback"] = False
    if getattr(args, "sem_master", False):
        updates["export_master"] = False
    resolucao = getattr(args, "resolucao", None)
    if resolucao and "x" in resolucao.lower():
        w, _, h = resolucao.lower().partition("x")
        try:
            updates["sequence_width"] = int(w)
            updates["sequence_height"] = int(h)
        except ValueError:
            pass
    cfg.update(updates)
    return cfg


# ----------------------------------------------------------------------
# comandos
# ----------------------------------------------------------------------

def cmd_organizar(args) -> int:
    state = ProjectState.load(args.projeto)
    state.name = args.projeto
    state.config = _config_from_args(args, state.config)
    session = Session(state=state)

    print(f"Varrendo {len(args.pastas)} pasta(s)...", flush=True)
    ultimo = -1

    try:
        session.scan_async(args.pastas)
        while session.is_scanning():
            pr = session.progress
            if pr.total and pr.done != ultimo:
                ultimo = pr.done
                bar = int(pr.done / pr.total * 30)
                sys.stdout.write(
                    f"\r  [{'#' * bar}{'.' * (30 - bar)}] {pr.done}/{pr.total} "
                    f"{format_bytes(pr.bytes_read)} lidos  ETA {pr.eta:5.1f}s  "
                    f"{pr.current[:28]:<28}"
                )
                sys.stdout.flush()
            time.sleep(0.1)
        sys.stdout.write("\n")
    except KeyboardInterrupt:
        session.cancel()
        print("\ncancelado — o que ja foi lido continua no cache.")
        return 130

    if session.progress.error:
        print(f"ERRO: {session.progress.error}", file=sys.stderr)
        return 1
    if session.progress.message:
        print(f"aviso: {session.progress.message}")

    project = session.project
    if project is None:
        print("nada para organizar.", file=sys.stderr)
        return 1

    day_keys: Optional[List[str]] = None
    if args.dias:
        day_keys = [d.strip() for d in args.dias.split(",") if d.strip()]

    xml_files: List[str] = []
    if not args.sem_exportar:
        result = session.export(
            args.saida, day_keys=day_keys,
            include_master=not args.sem_master,
            one_file_per_day=not args.arquivo_unico,
        )
        xml_files = result["files"]

    text = build_report(project, session.config, xml_files,
                        scan_seconds=project.read_seconds)
    if args.json:
        print(json.dumps(session.snapshot(), indent=2, ensure_ascii=False, default=str))
    else:
        print(text)
    session.close()
    return 0


def cmd_manifesto(args) -> int:
    from .manifest import build_manifest
    cfg = _config_from_args(args)
    print(f"Gerando manifesto em {args.pasta} ...")

    def progress(info):
        sys.stdout.write(f"\r  {info['done']}/{info['total']}  {info['current'][:40]:<40}")
        sys.stdout.flush()

    summary = build_manifest(args.pasta, config=cfg, progress=progress)
    sys.stdout.write("\n")
    print(f"manifesto: {summary['manifesto']}")
    print(f"arquivos:  {summary['arquivos']}")
    print(f"lidos:     {format_bytes(summary['bytes_lidos'])} de "
          f"{format_bytes(summary['bytes_totais'])}")
    if summary["sem_data"]:
        print(f"sem data ({len(summary['sem_data'])}): "
              f"{', '.join(summary['sem_data'][:10])}")
    print("\nSuba a pasta para o Drive com o manifesto.json dentro. "
          "Nas proximas vezes o app le so ele.")
    return 0


def cmd_calibrar(args) -> int:
    registry = DeviceRegistry()
    result = calibrate(args.arquivo_camera, args.arquivo_iphone, args.fuso,
                       refine_audio=not args.sem_audio, registry=registry)
    if not result.ok:
        print(f"ERRO: {result.message}", file=sys.stderr)
        return 1
    print(result.message)
    if result.note:
        print(result.note)
    profile = registry.get(result.device_key)
    if profile:
        print(f"salvo: {profile.display()}")
    return 0


def cmd_dispositivos(args) -> int:
    registry = DeviceRegistry()
    changed = False
    for item in args.renomear:
        key, _, label = item.partition("=")
        if registry.rename(key.strip(), label.strip()):
            changed = True
            print(f"renomeado: {key} → {label}")
        else:
            print(f"nao encontrei o perfil {key}", file=sys.stderr)
    for item in args.offset:
        key, _, value = item.partition("=")
        try:
            seconds = float(value)
        except ValueError:
            print(f"offset invalido: {value}", file=sys.stderr)
            continue
        if registry.set_manual_offset(key.strip(), seconds):
            changed = True
            print(f"offset manual de {key}: {format_offset(seconds)}")
    for key in args.apagar:
        if registry.delete(key.strip()):
            changed = True
            print(f"apagado: {key}")
    if not changed:
        if not registry.profiles:
            print("nenhum perfil salvo ainda. Use `calibrar` para cadastrar.")
        for key, profile in sorted(registry.profiles.items()):
            print(f"{key}")
            print(f"    {profile.display()}")
            if profile.manual_offset_seconds:
                print(f"    ajuste manual: {format_offset(profile.manual_offset_seconds)}")
            if profile.calibration_note:
                print(f"    {profile.calibration_note}")
    return 0


def cmd_web(args) -> int:
    from .web import serve
    return serve(port=args.porta, project=args.projeto, folders=args.pastas,
                 open_browser=not args.sem_navegador)


def cmd_cache(args) -> int:
    cache = MetadataCache()
    if args.limpar:
        cache.clear()
        print("cache limpo.")
    print(f"cache: {cache.count()} arquivos em {cache.path}")
    cache.close()
    return 0


def cmd_projetos(args) -> int:
    projects = list_projects()
    if not projects:
        print("nenhum projeto salvo.")
    for name in projects:
        state = ProjectState.load(name)
        print(f"{name}: {len(state.folders)} pasta(s), "
              f"{len(state.day_thresholds)} ajuste(s) manual(is) de limiar")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "organizar": cmd_organizar,
        "manifesto": cmd_manifesto,
        "calibrar": cmd_calibrar,
        "dispositivos": cmd_dispositivos,
        "web": cmd_web,
        "cache": cmd_cache,
        "projetos": cmd_projetos,
    }
    return handlers[args.comando](args)


if __name__ == "__main__":
    raise SystemExit(main())
