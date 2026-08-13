#!/usr/bin/env python3
"""Gerador parametrizavel de material sintetico para testar o Timeline Sync.

Nunca use material real para validar agrupamento: o que esta no HD nao foi
gravado no mesmo dia e nao prova nada. Aqui os horarios sao construidos de
proposito para cada armadilha.

Os videos sao leves (320x180, poucos segundos) mas o **metadado e realista**:

  * Sony   — `creation_time` com o valor literal do relogio, rotulado UTC ("Z"),
             e `moov/udta` com (c)mak/(c)mod. Opcionalmente um sidecar XML.
  * iPhone — `moov/meta` no padrao Apple, com `com.apple.quicktime.creationdate`
             trazendo o offset de fuso explicito, e o campo `...model`.
  * DJI    — como a Sony, com seu proprio erro de relogio.
  * Hollyland — WAV com chunk BWF `bext` (OriginationDate/OriginationTime).

O `ffmpeg` normaliza `creation_time` para UTC e descarta as chaves Apple, entao
os atoms sao injetados na mao por `timeline_sync.mp4write`.

Uso:
    python gerar_testes.py                      # gera todos os cenarios
    python gerar_testes.py --cenario 3 5        # so os cenarios 3 e 5
    python gerar_testes.py --saida /tmp/mat     # outra pasta
    python gerar_testes.py --escala 0.5         # metade dos arquivos
    python gerar_testes.py --listar
"""

from __future__ import annotations

import argparse
import os
import random
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from timeline_sync import mp4write
from timeline_sync.sidecar import write_sony_sidecar

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FUSO = timezone(timedelta(hours=-3))     # horario de Brasilia
SAIDA_PADRAO = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "material_teste")


# ----------------------------------------------------------------------
# dispositivos simulados
# ----------------------------------------------------------------------

@dataclass
class Dispositivo:
    nome: str
    padrao: str                # molde do nome do arquivo
    ext: str
    cor: str = "0x203040"
    make: str = ""
    model: str = ""
    audio: bool = True
    video: bool = True
    estilo: str = "sony"       # sony | apple | bwf
    # Erro do relogio interno em segundos: quanto o relogio da camera esta
    # adiantado em relacao a hora real.
    erro_relogio: float = 0.0
    sidecar: bool = False


def sony(erro: float = 0.0, sidecar: bool = False) -> Dispositivo:
    return Dispositivo("sony", "C{n:04d}", ".MP4", "0x1b3a5e", "Sony", "ILCE-7M3",
                       estilo="sony", erro_relogio=erro, sidecar=sidecar)


def iphone() -> Dispositivo:
    return Dispositivo("iphone", "IMG_{n:04d}", ".MOV", "0x5e3a1b", "Apple",
                       "iPhone 15 Pro", estilo="apple")


def dji(erro: float = 0.0) -> Dispositivo:
    return Dispositivo("dji", "DJI_{n:04d}", ".MP4", "0x1b5e3a", "DJI",
                       "Osmo Action 4", estilo="sony", erro_relogio=erro)


def hollyland() -> Dispositivo:
    return Dispositivo("hollyland", "HL_{n:04d}", ".WAV", "0x3a1b5e",
                       "Hollyland", "LARK M2", video=False, estilo="bwf")


# ----------------------------------------------------------------------
# eventos
# ----------------------------------------------------------------------

@dataclass
class Evento:
    dispositivo: Dispositivo
    inicio: datetime           # hora REAL de gravacao (local, com fuso)
    duracao: float
    rotulo: str = ""
    sem_data: bool = False     # simula arquivo sem creation_time
    # Instante (em segundos do inicio do clipe) de um transiente sonoro forte —
    # a palma/claquete que permite o refino de sync por audio.
    palma_em: Optional[float] = None


def takes(inicio: datetime, fim: datetime, dispositivos: Sequence[Dispositivo],
          intervalo: float = 150.0, duracao: Tuple[float, float] = (5.0, 9.0),
          jitter: float = 20.0, rng: Optional[random.Random] = None,
          rotulo: str = "") -> List[Evento]:
    """Sequencia de takes de `inicio` a `fim`, todos os dispositivos juntos.

    Os aparelhos comecam com 0-4s de diferenca entre si — de proposito, para
    provar que o agrupamento por take nao exige inicio no mesmo instante.
    """
    rng = rng or random.Random(7)
    eventos: List[Evento] = []
    t = inicio
    while t < fim:
        dur = rng.uniform(*duracao)
        for k, dev in enumerate(dispositivos):
            eventos.append(Evento(dev, t + timedelta(seconds=k * 1.7),
                                  dur + k * 0.8, rotulo))
        passo = intervalo + rng.uniform(-jitter, jitter)
        t += timedelta(seconds=max(passo, 20.0))
    return eventos


def dia(base: datetime, hora: float) -> datetime:
    h = int(hora)
    m = int(round((hora - h) * 60))
    return base.replace(hour=h % 24, minute=m, second=0, microsecond=0)


# ----------------------------------------------------------------------
# geracao dos arquivos
# ----------------------------------------------------------------------

def _drawtext_ok() -> bool:
    try:
        proc = subprocess.run(
            [FFMPEG, "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.1",
             "-vf", "drawtext=text=x:fontsize=10:fontcolor=white", "-f", "null", "-"],
            capture_output=True, timeout=60,
        )
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


_DRAWTEXT = None


def _audio_src(dev: Dispositivo, duracao: float,
               palma_em: Optional[float]) -> str:
    """Fonte de audio: tom continuo, com um estalo forte se houver palma."""
    freq = 440 + hash(dev.nome) % 300
    if palma_em is None:
        return f"sine=f={freq}:d={duracao:.2f}"
    # Estalo curto e alto em `palma_em`, sobre um tom baixo de fundo.
    return (
        f"aevalsrc='if(between(t,{palma_em:.3f},{palma_em + 0.06:.3f}),"
        f"0.95*sin(1800*2*PI*t),0.03*sin({freq}*2*PI*t))':d={duracao:.2f}:s=44100"
    )


def _gera_video(dest: str, dev: Dispositivo, duracao: float, texto: str,
                literal: datetime, palma_em: Optional[float] = None) -> None:
    global _DRAWTEXT
    if _DRAWTEXT is None:
        _DRAWTEXT = _drawtext_ok()

    filtro = f"color=c={dev.cor}:s=320x180:r=25:d={duracao:.2f}"
    cmd = [FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", filtro]
    if dev.audio:
        cmd += ["-f", "lavfi", "-i", _audio_src(dev, duracao, palma_em)]
    if _DRAWTEXT:
        legenda = texto.replace(":", "\\:").replace("'", "")
        cmd += ["-vf", (f"drawtext=text='{legenda}':fontsize=16:fontcolor=white:"
                        "x=10:y=70:box=1:boxcolor=black@0.5")]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-t", f"{duracao:.2f}"]
    if dev.audio:
        cmd += ["-c:a", "aac", "-b:a", "48k", "-shortest"]
    # `creation_time` rotulado UTC carregando o valor LITERAL do relogio —
    # exatamente o comportamento que quebra o agrupamento na vida real.
    cmd += ["-metadata", f"creation_time={literal.strftime('%Y-%m-%dT%H:%M:%S')}Z", dest]
    subprocess.run(cmd, check=True, capture_output=True, timeout=300)


def _gera_wav(dest: str, duracao: float, real: datetime, dev: Dispositivo) -> None:
    cmd = [FFMPEG, "-v", "error", "-y", "-f", "lavfi",
           "-i", f"sine=f=330:d={duracao:.2f}", "-ac", "2", "-ar", "48000",
           "-write_bext", "1", "-t", f"{duracao:.2f}", dest]
    subprocess.run(cmd, check=True, capture_output=True, timeout=300)
    _patch_bext(dest, real, dev.make or "Hollyland")


def _patch_bext(path: str, when: datetime, originator: str) -> None:
    """Preenche OriginationDate/Time no chunk `bext` (o ffmpeg deixa vazio)."""
    with open(path, "r+b") as fh:
        head = fh.read(65536)
        pos = head.find(b"bext")
        if pos < 0:
            return
        body = pos + 8
        fh.seek(body + 256)
        fh.write(originator.encode("ascii", "ignore")[:32].ljust(32, b"\x00"))
        fh.seek(body + 320)
        fh.write(when.strftime("%Y-%m-%d").encode("ascii"))
        fh.seek(body + 330)
        fh.write(when.strftime("%H:%M:%S").encode("ascii"))


def escreve_evento(evento: Evento, pasta: str, numero: int) -> str:
    dev = evento.dispositivo
    nome = dev.padrao.format(n=numero) + dev.ext
    dest = os.path.join(pasta, nome)
    real = evento.inicio
    literal = real + timedelta(seconds=dev.erro_relogio)

    if dev.estilo == "bwf":
        _gera_wav(dest, evento.duracao, real, dev)
        return dest

    texto = f"{nome}  {real.strftime('%d/%m %H:%M:%S')}  {evento.rotulo}"
    _gera_video(dest, dev, evento.duracao, texto, literal.replace(tzinfo=None),
                palma_em=evento.palma_em)

    if evento.sem_data:
        # zera o mvhd e nao injeta tag nenhuma: arquivo sem creation_time.
        mp4write.inject(dest, udta_tags=None, apple_items=None,
                        mvhd_epoch=-mp4write.QT_EPOCH_OFFSET)
        os.utime(dest, (real.timestamp(), real.timestamp()))
        return dest

    if dev.estilo == "apple":
        # O iPhone acerta a hora sozinho e grava o fuso explicitamente.
        mp4write.inject(
            dest,
            udta_tags={b"\xa9mak": dev.make, b"\xa9mod": dev.model},
            apple_items={
                "com.apple.quicktime.make": dev.make,
                "com.apple.quicktime.model": dev.model,
                "com.apple.quicktime.software": "18.2",
                "com.apple.quicktime.creationdate": _iso_tz(real),
            },
        )
    else:
        mp4write.inject(dest, udta_tags={b"\xa9mak": dev.make,
                                        b"\xa9mod": dev.model})
        if dev.sidecar:
            # Sidecar Sony carrega a hora REAL com fuso — poucos KB em vez de GB.
            write_sony_sidecar(dest, real, model=dev.model, make=dev.make)
    return dest


def _iso_tz(when: datetime) -> str:
    stamp = when.strftime("%Y-%m-%dT%H:%M:%S")
    off = when.utcoffset() or timedelta(0)
    total = int(off.total_seconds())
    sinal = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{stamp}{sinal}{total // 3600:02d}{(total % 3600) // 60:02d}"


# ----------------------------------------------------------------------
# cenarios
# ----------------------------------------------------------------------

@dataclass
class Cenario:
    numero: int
    slug: str
    titulo: str
    esperado: str
    construtor: Callable[[random.Random, float], List[Evento]]
    extras: Optional[Callable[[str, random.Random], List[str]]] = None


BASE = datetime(2026, 8, 13, tzinfo=FUSO)
ERRO_SONY = 3 * 3600 + 122        # 3h02min02s adiantada (comprada nos EUA)
ERRO_DJI = -1 * 3600 - 47         # DJI 1h00min47s atrasada


def _c1(rng: random.Random, escala: float) -> List[Evento]:
    """Fuso errado: Sony 3h adiantada em relacao ao iPhone do mesmo take."""
    s, i = sony(ERRO_SONY), iphone()
    ev = takes(dia(BASE, 10.0), dia(BASE, 10.0) + timedelta(minutes=int(40 * escala)),
               [s, i], intervalo=150, rng=rng, rotulo="fuso")
    ev += takes(dia(BASE, 14.0), dia(BASE, 14.0) + timedelta(minutes=int(30 * escala)),
                [s, i], intervalo=150, rng=rng, rotulo="fuso")
    return ev


def _c1_extras(pasta: str, rng: random.Random) -> List[str]:
    """Par de cadastro: os dois aparelhos gravando a mesma palma.

    A palma acontece as 09:00:03,0 (hora real). O iPhone comecou as 09:00:00 e a
    Sony 2,4s depois — entao o estalo cai em t=3,0s no clipe do iPhone e em
    t=0,6s no da Sony. Isso permite ao refino por audio zerar tambem o erro de
    segundos, chegando no erro real de relogio (+3h02min02s).
    """
    sub = os.path.join(pasta, "cadastro")
    os.makedirs(sub, exist_ok=True)
    momento = dia(BASE, 9.0)
    palma = 3.0
    atraso = 2.4
    s, i = sony(ERRO_SONY), iphone()
    a = escreve_evento(Evento(s, momento + timedelta(seconds=atraso), 8.0,
                              "cadastro", palma_em=palma - atraso), sub, 900)
    b = escreve_evento(Evento(i, momento, 8.0, "cadastro", palma_em=palma),
                       sub, 900)
    return [a, b]


def _c2(rng: random.Random, escala: float) -> List[Evento]:
    """Blocos obvios: 10:00-12:00, pausa de 2h, 14:00-16:00."""
    s, i, h = sony(), iphone(), hollyland()
    ev = takes(dia(BASE, 10.0), dia(BASE, 12.0), [s, i, h],
               intervalo=int(360 / escala), rng=rng, rotulo="bloco A")
    ev += takes(dia(BASE, 14.0), dia(BASE, 16.0), [s, i, h],
                intervalo=int(360 / escala), rng=rng, rotulo="bloco B")
    return ev


def _c3(rng: random.Random, escala: float) -> List[Evento]:
    """Pausa curta: mesma estrutura, mas o intervalo e de so ~20 min."""
    s, i = sony(), iphone()
    ev = takes(dia(BASE, 10.0), dia(BASE, 10.75), [s, i],
               intervalo=int(150 / escala), rng=rng, rotulo="bloco A")
    ev += takes(dia(BASE, 11.1), dia(BASE, 11.85), [s, i],
                intervalo=int(150 / escala), rng=rng, rotulo="bloco B")
    return ev


def _c4(rng: random.Random, escala: float) -> List[Evento]:
    """Dia continuo: 3h de takes uniformes, nenhuma pausa destacada."""
    s, i = sony(), iphone()
    return takes(dia(BASE, 9.0), dia(BASE, 12.0), [s, i],
                 intervalo=int(420 / escala), jitter=45, rng=rng, rotulo="continuo")


def _c5(rng: random.Random, escala: float) -> List[Evento]:
    """Semana completa: 5 dias, 2 a 4 blocos por dia, dispositivos variando."""
    s, i, d, h = sony(ERRO_SONY), iphone(), dji(ERRO_DJI), hollyland()
    plano = [
        (0, [(9.5, 10.4, [s, i, h]), (13.0, 14.2, [s, i]), (19.0, 19.7, [s])]),
        (1, [(10.0, 11.0, [s, i]), (15.0, 16.2, [s, i, d, h])]),
        (2, [(8.5, 9.3, [s, i, h]), (11.5, 12.3, [s, i]),
             (15.0, 15.8, [s, i, d]), (20.0, 20.6, [i])]),
        (3, [(9.0, 10.2, [s, i, h]), (14.5, 15.5, [s, i, h])]),
        (4, [(10.5, 11.4, [s, i, d]), (13.5, 14.3, [s, i]), (17.0, 18.0, [s, i, h])]),
    ]
    ev: List[Evento] = []
    for offset, blocos in plano:
        base = BASE + timedelta(days=offset)
        for k, (ini, fim, devs) in enumerate(blocos, start=1):
            ev += takes(dia(base, ini), dia(base, fim), devs,
                        intervalo=int(300 / escala), rng=rng,
                        rotulo=f"D{offset + 1}B{k}")
    return ev


def _c6(rng: random.Random, escala: float) -> List[Evento]:
    """Fragmentacao: muitos intervalos medios espalhados pelo dia.

    Deve acionar o teto de 6 blocos/dia e mesclar pelos menores intervalos.
    """
    s, i = sony(), iphone()
    ev: List[Evento] = []
    t = dia(BASE, 8.0)
    pausas = [14, 9, 22, 11, 17, 26, 8, 19, 13, 24]
    for k, pausa in enumerate(pausas, start=1):
        fim = t + timedelta(minutes=int(14 * escala) + 4)
        ev += takes(t, fim, [s, i], intervalo=int(170 / escala), rng=rng,
                    rotulo=f"frag {k}")
        t = fim + timedelta(minutes=pausa)
    return ev


def _c7(rng: random.Random, escala: float) -> List[Evento]:
    """Virada de madrugada: 22:00 -> 01:30 tem que ficar no mesmo dia."""
    s, i, h = sony(), iphone(), hollyland()
    ev = takes(dia(BASE, 22.0), dia(BASE, 23.5), [s, i, h],
               intervalo=int(420 / escala), rng=rng, rotulo="noite")
    madrugada = BASE + timedelta(days=1)
    ev += takes(dia(madrugada, 0.25), dia(madrugada, 1.5), [s, i, h],
                intervalo=int(420 / escala), rng=rng, rotulo="madrugada")
    return ev


def _c8(rng: random.Random, escala: float) -> List[Evento]:
    """Metadado ausente: um arquivo sem creation_time, sem quebrar a execucao."""
    s, i = sony(), iphone()
    ev = takes(dia(BASE, 10.0), dia(BASE, 10.6), [s, i],
               intervalo=int(200 / escala), rng=rng, rotulo="ok")
    ev += takes(dia(BASE, 14.0), dia(BASE, 14.6), [s, i],
                intervalo=int(200 / escala), rng=rng, rotulo="ok")
    orfao = Evento(s, dia(BASE, 10.3), 7.0, "SEM DATA", sem_data=True)
    ev.append(orfao)
    return ev


CENARIOS: List[Cenario] = [
    Cenario(1, "01_fuso_errado", "Fuso errado (Sony 3h adiantada)",
            "a calibracao contra o iPhone corrige e os takes casam", _c1, _c1_extras),
    Cenario(2, "02_blocos_obvios", "Blocos obvios (pausa de 2h)",
            "exatamente 2 blocos", _c2),
    Cenario(3, "03_pausa_curta", "Pausa curta (~20 min)",
            "ainda 2 blocos — prova que o limiar e relativo", _c3),
    Cenario(4, "04_dia_continuo", "Dia continuo (3h uniformes)",
            "1 bloco so, sem divisao inventada", _c4),
    Cenario(5, "05_semana_completa", "Semana completa (5 dias)",
            "5 dias, 2-4 blocos cada, cores reiniciando por dia", _c5),
    Cenario(6, "06_fragmentacao", "Fragmentacao excessiva",
            "teto de 6 blocos/dia acionado", _c6),
    Cenario(7, "07_virada_madrugada", "Virada de madrugada (22h -> 01:30)",
            "tudo num unico dia", _c7),
    Cenario(8, "08_metadado_ausente", "Metadado ausente",
            "fallback sinalizado, execucao inteira", _c8),
]


# ----------------------------------------------------------------------
# execucao
# ----------------------------------------------------------------------

def gera_cenario(cenario: Cenario, saida: str, escala: float = 1.0,
                 semente: int = 7, verbose: bool = True) -> Dict[str, object]:
    rng = random.Random(semente + cenario.numero)
    pasta = os.path.join(saida, cenario.slug)
    if os.path.isdir(pasta):
        shutil.rmtree(pasta)
    os.makedirs(pasta, exist_ok=True)

    eventos = cenario.construtor(rng, escala)
    eventos.sort(key=lambda e: e.inicio)

    contadores: Dict[str, int] = {}
    caminhos: List[str] = []
    for evento in eventos:
        nome = evento.dispositivo.nome
        contadores[nome] = contadores.get(nome, 0) + 1
        numero = contadores[nome] + (20 if nome == "sony" else 0)
        caminhos.append(escreve_evento(evento, pasta, numero))
        if verbose and len(caminhos) % 25 == 0:
            sys.stdout.write(f"\r  {cenario.slug}: {len(caminhos)}/{len(eventos)}")
            sys.stdout.flush()

    extras: List[str] = []
    if cenario.extras is not None:
        extras = cenario.extras(pasta, rng)

    if verbose:
        sys.stdout.write(f"\r  {cenario.slug}: {len(caminhos)} arquivos"
                         + (f" + {len(extras)} de cadastro" if extras else "")
                         + " " * 20 + "\n")

    total = sum(os.path.getsize(p) for p in caminhos + extras)
    info = {
        "numero": cenario.numero,
        "slug": cenario.slug,
        "titulo": cenario.titulo,
        "esperado": cenario.esperado,
        "pasta": pasta,
        "arquivos": len(caminhos),
        "extras": extras,
        "bytes": total,
    }
    return info


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--saida", "-o", default=SAIDA_PADRAO)
    p.add_argument("--cenario", "-c", nargs="*", type=int,
                   help="numeros dos cenarios (padrao: todos)")
    p.add_argument("--escala", type=float, default=1.0,
                   help="densidade de takes (0.5 = metade dos arquivos)")
    p.add_argument("--semente", type=int, default=7)
    p.add_argument("--listar", action="store_true")
    args = p.parse_args(argv)

    if args.listar:
        for c in CENARIOS:
            print(f"{c.numero}. {c.titulo}\n   esperado: {c.esperado}")
        return 0

    if not shutil.which(FFMPEG):
        print("ffmpeg nao encontrado no PATH.", file=sys.stderr)
        return 1

    escolhidos = [c for c in CENARIOS
                  if not args.cenario or c.numero in args.cenario]
    os.makedirs(args.saida, exist_ok=True)
    print(f"Gerando {len(escolhidos)} cenario(s) em {args.saida}")
    total_arquivos = 0
    total_bytes = 0
    for cenario in escolhidos:
        info = gera_cenario(cenario, args.saida, escala=args.escala,
                            semente=args.semente)
        total_arquivos += int(info["arquivos"]) + len(info["extras"])
        total_bytes += int(info["bytes"])
    print(f"\n{total_arquivos} arquivos, {total_bytes / 1024 / 1024:.1f} MB em "
          f"{args.saida}")
    print("Rode o pipeline com:  python rodar_cenarios.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
