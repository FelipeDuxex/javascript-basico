#!/usr/bin/env python3
"""Roda o pipeline completo nos 8 cenarios sinteticos e valida o resultado.

Cada cenario foi construido para uma armadilha especifica; aqui cada um vira uma
assercao. O cache e isolado por cenario, para a metrica de bytes lidos ser real
(com cache quente ela seria zero e nao provaria nada).

    python rodar_cenarios.py            # gera o material se faltar e valida tudo
    python rodar_cenarios.py --gerar    # regera o material antes
    python rodar_cenarios.py -c 3 5     # so os cenarios 3 e 5
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from xml.etree import ElementTree

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gerar_testes
from timeline_sync.cache import MetadataCache
from timeline_sync.colors import VALID_LABELS, block_label
from timeline_sync.config import Config, ProjectState
from timeline_sync.devices import DeviceRegistry, calibrate
from timeline_sync.models import Project
from timeline_sync.pipeline import Session
from timeline_sync.timeutil import format_bytes, format_gap, format_offset

RAIZ = os.path.dirname(os.path.abspath(__file__))
MATERIAL = os.path.join(RAIZ, "material_teste")
SAIDA = os.path.join(RAIZ, "exemplos")
LOG = os.path.join(SAIDA, "log_cenarios.txt")


class Falha(AssertionError):
    pass


@dataclass
class Resultado:
    numero: int
    titulo: str
    ok: bool = True
    linhas: List[str] = field(default_factory=list)
    erros: List[str] = field(default_factory=list)

    def diz(self, texto: str) -> None:
        self.linhas.append(texto)

    def checa(self, condicao: bool, descricao: str) -> None:
        marca = "OK  " if condicao else "FALHA"
        self.linhas.append(f"    [{marca}] {descricao}")
        if not condicao:
            self.ok = False
            self.erros.append(descricao)


# ----------------------------------------------------------------------
# infraestrutura
# ----------------------------------------------------------------------

def registry_limpo() -> DeviceRegistry:
    """Registro de dispositivos isolado.

    Cada cenario simula a MESMA Sony com um erro de relogio diferente, entao um
    registro compartilhado faria a calibracao de um cenario vazar nos outros — e
    o teste passaria ou falharia dependendo da ordem de execucao.
    """
    return DeviceRegistry(path=os.path.join(tempfile.mkdtemp(prefix="ts-dev-"),
                                            "dispositivos.json"))


def sessao(pasta: str, nome: str, ajustes: Optional[Dict] = None,
           registry: Optional[DeviceRegistry] = None) -> Tuple[Session, Project]:
    """Sessao limpa, com cache e registro proprios, para o cenario indicado."""
    tmp = tempfile.mkdtemp(prefix="ts-cache-")
    cache = MetadataCache(path=os.path.join(tmp, "cache.sqlite"))
    state = ProjectState(name=nome, config=Config())
    if ajustes:
        state.config.update(ajustes)
    session = Session(state=state, registry=registry or registry_limpo(),
                      cache=cache)
    project = session.scan_sync([pasta])
    return session, project


def exporta(session: Session, slug: str) -> Dict:
    destino = os.path.join(SAIDA, slug)
    if os.path.isdir(destino):
        shutil.rmtree(destino)
    return session.export(destino)


def valida_xml(caminho: str, res: Resultado) -> Dict[str, int]:
    """Confere que o XML e bem formado e que a estrutura xmeml esta correta."""
    tree = ElementTree.parse(caminho)
    root = tree.getroot()
    res.checa(root.tag == "xmeml", f"{os.path.basename(caminho)}: raiz e <xmeml>")
    res.checa(root.get("version") == "4", "xmeml version=4")

    sequences = root.findall(".//sequence")
    clipitems = root.findall(".//sequence//clipitem")
    markers = root.findall(".//sequence/marker")
    labels = [el.text for el in root.findall(".//label2")]
    invalidas = sorted({l for l in labels if l not in VALID_LABELS})
    res.checa(not invalidas,
              f"todas as cores sao labels validas do Premiere "
              f"({len(set(labels))} usadas){' — invalidas: ' + str(invalidas) if invalidas else ''}")

    files = root.findall(".//file[pathurl]")
    res.checa(all(f.find("pathurl").text.startswith("file://") for f in files),
              f"{len(files)} pathurl no formato file://")
    vtracks = root.findall(".//sequence/media/video/track")
    atracks = root.findall(".//sequence/media/audio/track")
    bins = root.findall(".//bin")
    return {
        "sequences": len(sequences), "clipitems": len(clipitems),
        "markers": len(markers), "video_tracks": len(vtracks),
        "audio_tracks": len(atracks), "bins": len(bins),
        "labels": len(set(labels)),
    }


def resume(res: Resultado, project: Project) -> None:
    res.diz(f"    {project.total_files} arquivos | {len(project.devices)} dispositivos "
            f"| {len(project.days)} dia(s) | {project.block_count} bloco(s) "
            f"| {project.take_count} take(s)")
    pct = (project.bytes_read / project.total_bytes * 100) if project.total_bytes else 0
    res.diz(f"    leitura parcial: {format_bytes(project.bytes_read)} de "
            f"{format_bytes(project.total_bytes)} ({pct:.3f}%) via "
            f"{project.read_methods}")
    for dia in project.days:
        limiar = (format_gap(dia.threshold_seconds) if dia.threshold_seconds
                  else "bloco unico")
        res.diz(f"    {dia.label()} {dia.key}: {len(dia.blocks)} bloco(s), "
                f"{len(dia.clips)} clipes, limiar {limiar} "
                f"({dia.threshold_source})")
        for b in dia.blocks:
            res.diz(f"        {b.label()} {b.start.strftime('%H:%M')}-"
                    f"{b.end.strftime('%H:%M')} | {len(b.clips)} clipes | "
                    f"{len(b.takes)} takes | cor {block_label(b.index)}")
        if dia.detection_note:
            res.diz(f"        -> {dia.detection_note}")


# ----------------------------------------------------------------------
# cenarios
# ----------------------------------------------------------------------

def cenario_1(pasta: str, res: Resultado) -> None:
    """Fuso errado: a calibracao contra o iPhone tem que corrigir o desvio."""
    registry = registry_limpo()
    cad = os.path.join(pasta, "cadastro")
    camera = os.path.join(cad, "C0900.MP4")
    ref = os.path.join(cad, "IMG_0900.MOV")

    # -- antes da calibracao: os horarios NAO batem -----------------------
    _, antes = sessao(pasta, "c1-antes", registry=registry)
    takes_antes = antes.take_count
    orfaos_antes = len(antes.orphan_takes())
    res.diz(f"    antes de calibrar: {takes_antes} takes, {orfaos_antes} orfaos")
    res.checa(orfaos_antes > 0,
              "sem calibracao os takes NAO casam (Sony 3h fora) — orfaos aparecem")
    res.checa(any("nao esta calibrado" in w for w in antes.warnings),
              "avisa que a Sony nao esta calibrada")

    # -- calibracao ------------------------------------------------------
    cal = calibrate(camera, ref, "America/Sao_Paulo", refine_audio=True,
                    registry=registry)
    res.checa(cal.ok, f"calibracao concluida: {cal.message}")
    erro_real = -gerar_testes.ERRO_SONY
    res.checa(abs(cal.offset_seconds - erro_real) < 1.5,
              f"offset calculado {format_offset(cal.offset_seconds)} bate com o erro "
              f"real {format_offset(erro_real)} (tolerancia 1,5s)")
    res.checa(cal.refined_by_audio,
              f"refino por audio entrou em acao ({cal.audio_shift_seconds:+.2f}s)")

    # -- depois: os takes casam ------------------------------------------
    session, depois = sessao(pasta, "c1", registry=registry)
    resume(res, depois)
    principais = [t for d in depois.days for b in d.blocks for t in b.takes
                  if b.start and b.start.hour >= 10]
    completos = [t for t in principais if len(t.devices) >= 2]
    res.checa(len(completos) == len(principais) and principais,
              f"depois de calibrar, os {len(principais)} takes de gravacao juntam "
              "Sony + iPhone")
    desvios = []
    for take in completos:
        tempos = [c.start for c in take.clips if c.start]
        desvios.append((max(tempos) - min(tempos)).total_seconds())
    res.checa(desvios and max(desvios) <= 10.0,
              f"maior desvio dentro de um take: {max(desvios):.1f}s (janela de 10s)")
    res.checa(not any("nao esta calibrado" in w and "Sony" in w
                      for w in depois.warnings),
              "o aviso de 'nao calibrado' desaparece para a Sony")

    info = valida_xml(exporta(session, "01_fuso_errado")["files"][0], res)
    res.diz(f"    XML: {info}")


def cenario_2(pasta: str, res: Resultado) -> None:
    """Blocos obvios: pausa de 2h no meio do dia."""
    session, project = sessao(pasta, "c2")
    resume(res, project)
    res.checa(len(project.days) == 1, "1 dia detectado")
    res.checa(project.block_count == 2, f"exatamente 2 blocos (deu {project.block_count})")
    dia = project.days[0]
    res.checa(dia.blocks[0].start.hour == 10 and dia.blocks[1].start.hour == 14,
              "os blocos comecam as 10h e as 14h")
    res.checa(dia.threshold_source.startswith("auto"),
              f"limiar decidido automaticamente ({dia.threshold_source})")
    # A Hollyland (WAV/BWF) tem que estar presente e numa trilha de audio.
    audio_only = [c for c in project.clips if c.is_audio_only]
    res.checa(bool(audio_only), f"{len(audio_only)} clipes de audio externo lidos")
    res.checa(all(c.time_source == "bwf.bext" for c in audio_only),
              "o horario dos WAV veio do chunk BWF bext (nao de mtime)")

    exp = exporta(session, "02_blocos_obvios")
    info = valida_xml(exp["files"][0], res)
    res.diz(f"    XML: {info}")
    res.checa(info["markers"] >= 2, "marcadores de bloco no XML")
    res.checa(info["audio_tracks"] >= 3,
              f"trilhas de audio separadas por dispositivo ({info['audio_tracks']})")


def cenario_3(pasta: str, res: Resultado) -> None:
    """Pausa curta: 20 min. Um limiar fixo de 30 min erraria aqui."""
    session, project = sessao(pasta, "c3")
    resume(res, project)
    res.checa(project.block_count == 2,
              f"2 blocos mesmo com pausa de ~20 min (deu {project.block_count})")
    dia = project.days[0]
    res.checa(dia.threshold_seconds is not None and dia.threshold_seconds < 1800,
              f"o limiar escolhido ({format_gap(dia.threshold_seconds or 0)}) ficou "
              "abaixo dos 30 min de um limiar fixo — e relativo, nao fixo")

    # Contraprova: com limiar fixo de 30 min, o mesmo material da 1 bloco.
    _, fixo = sessao(pasta, "c3-fixo", {"block_method": "fixo",
                                        "block_fixed_threshold_seconds": 1800})
    res.checa(fixo.block_count == 1,
              f"contraprova: limiar fixo de 30 min daria {fixo.block_count} bloco — "
              "e por isso que a deteccao e adaptativa")
    valida_xml(exporta(session, "03_pausa_curta")["files"][0], res)


def cenario_4(pasta: str, res: Resultado) -> None:
    """Dia continuo: nao inventar divisao onde nao ha pausa."""
    session, project = sessao(pasta, "c4")
    resume(res, project)
    res.checa(project.block_count == 1,
              f"1 bloco so (deu {project.block_count}) — nenhuma divisao artificial")
    dia = project.days[0]
    res.checa(dia.threshold_seconds is None,
              "nenhum limiar aplicado: o dia foi classificado como uniforme")
    res.checa("uniforme" in dia.detection_note or "parecidos" in dia.detection_note,
              f"a nota explica a decisao: {dia.detection_note}")
    valida_xml(exporta(session, "04_dia_continuo")["files"][0], res)


def cenario_5(pasta: str, res: Resultado) -> None:
    """Semana completa: dia antes de bloco, e cores reiniciando por dia."""
    registry = registry_limpo()
    # A Sony e a DJI tem relogio errado; cadastramos as duas pelos offsets
    # conhecidos (na vida real isso vem do comando `calibrar`).
    from timeline_sync.reader import read_one
    import glob
    amostra_sony = sorted(glob.glob(os.path.join(pasta, "C*.MP4")))[0]
    amostra_dji = sorted(glob.glob(os.path.join(pasta, "DJI_*.MP4")))[0]
    for amostra, erro in ((amostra_sony, gerar_testes.ERRO_SONY),
                          (amostra_dji, gerar_testes.ERRO_DJI)):
        clip = read_one(amostra)
        registry.apply_calibration(clip.device_key, -erro, "cenario 5",
                                   kind=clip.device_kind, make=clip.make,
                                   model=clip.model)

    session, project = sessao(pasta, "c5", registry=registry)
    resume(res, project)

    res.checa(len(project.days) == 5, f"5 dias separados (deu {len(project.days)})")
    contagens = [len(d.blocks) for d in project.days]
    res.checa(all(2 <= n <= 4 for n in contagens),
              f"cada dia com 2 a 4 blocos: {contagens}")
    res.checa(not any(n == 1 for n in contagens),
              "nenhum dia virou bloco unico por engano — a deteccao rodou DENTRO "
              "de cada dia, nao sobre a semana")

    # Contraprova da regra critica: rodando a deteccao sobre a semana inteira
    # como se fosse um unico dia, os intervalos noturnos dominam e cada dia vira
    # um bloco so.
    juntos = _blocos_ignorando_dia(project, session)
    res.checa(juntos <= len(project.days) + 1,
              f"contraprova: sem separar por dia primeiro, a semana daria apenas "
              f"{juntos} bloco(s) — as pausas internas desapareceriam")

    # cores reiniciam a cada dia
    primeiras = {d.key: block_label(d.blocks[0].index) for d in project.days}
    res.checa(len(set(primeiras.values())) == 1,
              f"o 1o bloco de todo dia tem a mesma cor ({set(primeiras.values())}) "
              "— a paleta reinicia por dia")
    consecutivas = []
    for d in project.days:
        cores = [block_label(b.index) for b in d.blocks]
        consecutivas += [cores[i] == cores[i + 1] for i in range(len(cores) - 1)]
    res.checa(not any(consecutivas), "blocos consecutivos nunca repetem cor")

    dispositivos = {d.label for d in project.devices}
    res.checa(len(dispositivos) >= 4,
              f"dispositivos variando entre os dias: {sorted(dispositivos)}")
    res.checa(not any("nao se cruzam" in w for w in project.warnings),
              "sanidade: nenhum dispositivo ficou isolado em datas proprias")

    exp = exporta(session, "05_semana_completa")
    res.checa(len(exp["files"]) == 6,
              f"6 XMLs gerados: 5 dias + MASTER (deu {len(exp['files'])})")
    total_clipes = 0
    for caminho in exp["files"]:
        info = valida_xml(caminho, res)
        total_clipes += info["clipitems"]
        if "MASTER" in caminho:
            res.diz(f"    MASTER: {info}")
            res.checa(info["markers"] >= 5 + 5,
                      f"MASTER tem marcador de dia e de bloco ({info['markers']})")
    res.diz(f"    total de clipitems nos XMLs: {total_clipes}")


def _blocos_ignorando_dia(project: Project, session: Session) -> int:
    """Quantos blocos sairiam se a deteccao rodasse sobre a semana inteira."""
    from timeline_sync.grouping import build_day
    todos = sorted(project.clips, key=lambda c: c.start or datetime.min)
    fingido = build_day("semana-inteira", 1, todos, session.config, None)
    return len(fingido.blocks)


def cenario_6(pasta: str, res: Resultado) -> None:
    """Fragmentacao: o teto de blocos por dia tem que entrar em acao."""
    session, project = sessao(pasta, "c6")
    resume(res, project)
    dia = project.days[0]
    res.checa(len(dia.blocks) <= 6,
              f"teto de 6 blocos/dia respeitado (deu {len(dia.blocks)})")

    # Sem teto, o mesmo material se fragmenta em muito mais blocos.
    _, solto = sessao(pasta, "c6-solto", {"block_max_per_day": 99})
    res.diz(f"    sem teto: {len(solto.days[0].blocks)} blocos")
    res.checa(len(solto.days[0].blocks) > 6,
              f"sem teto daria {len(solto.days[0].blocks)} blocos — o teto e o que "
              "mantem a visualizacao util")

    # E com teto baixo, a mesclagem iterativa e acionada explicitamente.
    _, apertado = sessao(pasta, "c6-apertado", {"block_max_per_day": 3})
    res.checa(len(apertado.days[0].blocks) == 3,
              f"com teto de 3, mescla pelos menores intervalos ate 3 blocos "
              f"(deu {len(apertado.days[0].blocks)})")
    res.checa(any("teto de" in d.detection_note for d in apertado.days),
              "a nota registra que o teto foi acionado")
    valida_xml(exporta(session, "06_fragmentacao")["files"][0], res)


def cenario_7(pasta: str, res: Resultado) -> None:
    """Virada de madrugada: 22:00 -> 01:30 e um dia so."""
    session, project = sessao(pasta, "c7")
    resume(res, project)
    res.checa(len(project.days) == 1,
              f"tudo num unico dia (deu {len(project.days)})")
    res.checa(project.days[0].key == "2026-08-13",
              f"o dia e o da virada (2026-08-13), nao o seguinte "
              f"(deu {project.days[0].key})")
    horas = {c.start.hour for c in project.clips}
    res.checa(bool({22, 23} & horas) and bool({0, 1} & horas),
              f"o mesmo dia contem noite e madrugada: horas {sorted(horas)}")

    # Contraprova: com virada a meia-noite, o material se parte em dois.
    _, meianoite = sessao(pasta, "c7-meianoite", {"day_start_hour": 0})
    res.checa(len(meianoite.days) == 2,
              f"contraprova: virada a meia-noite partiria em "
              f"{len(meianoite.days)} dias")
    valida_xml(exporta(session, "07_virada_madrugada")["files"][0], res)


def cenario_8(pasta: str, res: Resultado) -> None:
    """Metadado ausente: fallback sinalizado, sem quebrar a execucao."""
    session, project = sessao(pasta, "c8")
    resume(res, project)
    res.checa(len(project.fallback_clips) == 1,
              f"exatamente 1 arquivo caiu no fallback "
              f"(deu {len(project.fallback_clips)})")
    if project.fallback_clips:
        clip = project.fallback_clips[0]
        res.diz(f"    fallback: {clip.filename} via {clip.time_source} "
                f"→ {clip.start} (confianca {clip.trust})")
        res.checa(clip.time_source == "mtime", "a fonte registrada e mtime")
        res.checa(bool(clip.warnings), f"o clipe carrega aviso: {clip.warnings[0]}")
        res.checa(clip.start is not None,
                  "mesmo assim ele foi posicionado na timeline")
        # O mtime foi gravado as 10:18 locais, mas e lido no fuso do sistema —
        # o clipe cai longe do lugar certo e abre um bloco proprio. Isso NAO e
        # um defeito: e a demonstracao de que mtime nao serve como horario de
        # gravacao, e por isso ele e o ultimo recurso e vem sinalizado.
        bloco_do_orfao = next((b for d in project.days for b in d.blocks
                               if clip in b.clips), None)
        res.checa(bloco_do_orfao is not None and len(bloco_do_orfao.clips) == 1,
                  f"o clipe de fallback ficou visivelmente isolado em "
                  f"{bloco_do_orfao.label() if bloco_do_orfao else '?'} — prova de "
                  "que mtime posiciona errado, e de que da para enxergar isso")
    res.checa(project.total_files == len(project.clips) + len(project.unknown_time_clips),
              "nenhum arquivo foi perdido no caminho")

    # Na nuvem o mtime e enganoso: o fallback tem que ser desligado e o clipe
    # marcado como data desconhecida.
    _, nuvem = sessao(pasta, "c8-nuvem", {"use_mtime_fallback": False})
    res.checa(len(nuvem.unknown_time_clips) == 1,
              f"com o fallback desligado (regra do Drive), o arquivo vira 'data "
              f"desconhecida' (deu {len(nuvem.unknown_time_clips)})")
    res.checa(any("data desconhecida" in w or "sem data" in w
                  for c in nuvem.unknown_time_clips for w in c.warnings),
              "e o motivo aparece no aviso do clipe")

    exp = exporta(session, "08_metadado_ausente")
    res.checa("FALLBACK" in open(exp["files"][0], encoding="utf-8").read(),
              "o XML marca o clipe de fallback nos comentarios do clipe")
    valida_xml(exp["files"][0], res)


# ----------------------------------------------------------------------
# teste de leitura parcial em arquivo grande
# ----------------------------------------------------------------------

def teste_leitura_parcial(res: Resultado) -> None:
    """Prova, em bytes, que o app nao arrasta o arquivo inteiro.

    Monta um MP4 esparso de 3 GB (com o `mdat` cheio de zeros que nem existem em
    disco) e mede quantos bytes o leitor efetivamente tocou — nas duas
    disposicoes possiveis do `moov`: no fim (padrao) e no inicio (faststart).
    """
    from timeline_sync import mp4reader, mp4write
    import glob

    origem = sorted(glob.glob(os.path.join(MATERIAL, "01_fuso_errado", "C*.MP4")))
    if not origem:
        res.checa(False, "material do cenario 1 necessario para este teste")
        return
    tmp = tempfile.mkdtemp(prefix="ts-grande-")
    alvo = 3 * 1024 * 1024 * 1024      # 3 GB

    # moov no fim (como a maioria das cameras grava)
    fim = os.path.join(tmp, "GRANDE_moov_no_fim.mp4")
    mp4write.make_huge_stub(origem[0], fim, alvo)
    meta_fim = mp4reader.read_metadata(fim)

    # moov no inicio (faststart)
    inicio = os.path.join(tmp, "GRANDE_moov_no_inicio.mp4")
    _monta_faststart(origem[0], inicio, alvo)
    meta_ini = mp4reader.read_metadata(inicio)

    for nome, meta in (("moov no fim", meta_fim), ("moov no inicio", meta_ini)):
        tamanho = os.path.getsize(meta.path)
        pct = meta.bytes_read / tamanho * 100
        res.diz(f"    {nome}: arquivo de {format_bytes(tamanho)}, lidos "
                f"{format_bytes(meta.bytes_read)} em {meta.reads} leituras "
                f"({pct:.6f}%) → creation_time {meta.mvhd_time_literal}")
        res.checa(meta.ok and meta.mvhd_time_literal is not None,
                  f"{nome}: leu a data sem abrir o arquivo todo")
        res.checa(meta.bytes_read < 1024 * 1024,
                  f"{nome}: leu menos de 1 MB ({format_bytes(meta.bytes_read)}) de "
                  f"{format_bytes(tamanho)}")

    # E o sidecar e ainda mais barato: nem toca no video.
    from timeline_sync import sidecar as sc
    from datetime import timezone, timedelta as td
    quando = datetime(2026, 8, 13, 10, 3, tzinfo=timezone(td(hours=-3)))
    sc.write_sony_sidecar(fim, quando)
    info = sc.read_sidecar(fim)
    res.checa(info is not None and info.time is not None,
              f"sidecar lido em {info.bytes_read if info else 0} bytes, sem tocar "
              "no arquivo de 3 GB")

    shutil.rmtree(tmp, ignore_errors=True)


def _monta_faststart(origem: str, destino: str, total: int) -> None:
    """ftyp + moov + mdat esparso — o layout de um arquivo com faststart."""
    import struct
    from timeline_sync.mp4reader import iter_atoms_buffer
    with open(origem, "rb") as fh:
        buf = fh.read()
    tops = list(iter_atoms_buffer(buf))
    ftyp = next((a for a in tops if a.type == b"ftyp"), None)
    moov = next((a for a in tops if a.type == b"moov"), None)
    head = buf[ftyp.offset:ftyp.end] if ftyp else b""
    moov_bytes = buf[moov.offset:moov.end]
    mdat_size = total - len(head) - len(moov_bytes)
    with open(destino, "wb") as fh:
        fh.write(head)
        fh.write(moov_bytes)
        fh.write(struct.pack(">I", 1) + b"mdat" + struct.pack(">Q", mdat_size))
        fh.seek(total - 1)
        fh.write(b"\x00")


# ----------------------------------------------------------------------
# teste do modo manifesto
# ----------------------------------------------------------------------

def teste_manifesto(res: Resultado) -> None:
    """O manifesto tem que zerar o I/O na midia nas execucoes seguintes."""
    from timeline_sync.manifest import build_manifest
    pasta = os.path.join(MATERIAL, "02_blocos_obvios")
    if not os.path.isdir(pasta):
        res.checa(False, "material do cenario 2 necessario")
        return
    copia = tempfile.mkdtemp(prefix="ts-manifesto-")
    destino = os.path.join(copia, "material")
    shutil.copytree(pasta, destino)

    resumo = build_manifest(destino, config=Config())
    res.diz(f"    manifesto: {resumo['arquivos']} arquivos, "
            f"{format_bytes(resumo['bytes_lidos'])} lidos na geracao")
    res.checa(os.path.isfile(os.path.join(destino, "manifesto.json")),
              "manifesto.json criado na pasta do material")

    _, project = sessao(destino, "manifesto")
    res.diz(f"    com manifesto: {project.read_methods}, "
            f"{format_bytes(project.bytes_read)} lidos")
    res.checa(project.bytes_read == 0,
              f"nenhum byte de midia transferido na 2a leitura "
              f"(deu {project.bytes_read})")
    res.checa(project.read_methods.get("manifesto", 0) == project.total_files,
              f"todos os {project.total_files} arquivos vieram do manifesto")
    shutil.rmtree(copia, ignore_errors=True)


def teste_cache(res: Resultado) -> None:
    """O cache tem que dispensar a releitura, e a chave nao usa mtime."""
    pasta = os.path.join(MATERIAL, "03_pausa_curta")
    if not os.path.isdir(pasta):
        res.checa(False, "material do cenario 3 necessario")
        return
    tmp = tempfile.mkdtemp(prefix="ts-cache-")
    cache = MetadataCache(path=os.path.join(tmp, "c.sqlite"))
    state = ProjectState(name="cache", config=Config())
    s1 = Session(state=state, registry=DeviceRegistry(), cache=cache)
    p1 = s1.scan_sync([pasta])
    res.diz(f"    1a passada: {format_bytes(p1.bytes_read)} lidos, {p1.read_methods}")

    # Mexer no mtime NAO pode invalidar o cache (no Drive ele muda sozinho).
    import glob
    for caminho in glob.glob(os.path.join(pasta, "*.MP4"))[:5]:
        os.utime(caminho, (0, 0))

    s2 = Session(state=ProjectState(name="cache2", config=Config()),
                 registry=DeviceRegistry(), cache=cache)
    p2 = s2.scan_sync([pasta])
    res.diz(f"    2a passada: {format_bytes(p2.bytes_read)} lidos, {p2.read_methods}")
    res.checa(p2.bytes_read == 0, "2a passada nao transferiu nada")
    res.checa(p2.read_methods.get("cache", 0) == p2.total_files,
              "todos os arquivos vieram do cache mesmo com o mtime alterado")
    cache.close()
    shutil.rmtree(tmp, ignore_errors=True)


def teste_tempo_morto(res: Resultado) -> None:
    """Os tres modos de tempo morto tem que produzir duracoes diferentes."""
    pasta = os.path.join(MATERIAL, "02_blocos_obvios")
    if not os.path.isdir(pasta):
        res.checa(False, "material do cenario 2 necessario")
        return
    duracoes = {}
    for modo in ("preserve", "compress", "close"):
        session, project = sessao(pasta, f"gap-{modo}", {"gap_mode": modo})
        from timeline_sync.timeline import build_day_sequence
        seq = build_day_sequence(project.days[0], session.config)
        duracoes[modo] = seq.duration
        res.diz(f"    {modo}: timeline de {seq.duration / 3600:.2f}h "
                f"(tempo morto encurtado: {seq.removed_seconds / 3600:.2f}h)")
    res.checa(duracoes["preserve"] > duracoes["compress"] > duracoes["close"],
              f"preserve > compress > close: "
              f"{ {k: round(v) for k, v in duracoes.items()} }")
    res.checa(duracoes["preserve"] > 5 * 3600,
              "no modo preserve a pausa de 2h aparece inteira na timeline")


def teste_refino_audio(res: Resultado) -> None:
    """O refino por audio: os dois caminhos tem que concordar, e recusar ruido.

    Guarda dois defeitos ja corrigidos: (a) numpy e Python puro usavam
    normalizacoes diferentes e podiam devolver respostas distintas; (b) a funcao
    respondia com confianca sobre audio sem transiente nenhum, onde a correlacao
    e so ruido.
    """
    import glob

    from timeline_sync import audiosync

    if not audiosync.available():
        res.diz("    (ffmpeg ausente — refino por audio nao pode ser testado)")
        return

    cad = os.path.join(MATERIAL, "01_fuso_errado", "cadastro")
    com_palma = (os.path.join(cad, "IMG_0900.MOV"), os.path.join(cad, "C0900.MP4"))
    planos_dir = os.path.join(MATERIAL, "02_blocos_obvios")
    planos = (sorted(glob.glob(os.path.join(planos_dir, "IMG_*.MOV")))[:1],
              sorted(glob.glob(os.path.join(planos_dir, "C*.MP4")))[:1])

    def medir(ref, outro, forcar_puro):
        original = audiosync._numpy
        if forcar_puro:
            audiosync._numpy = lambda: None
        try:
            return audiosync.cross_correlate(ref, outro)
        finally:
            audiosync._numpy = original

    if all(os.path.isfile(p) for p in com_palma):
        com_np = medir(*com_palma, forcar_puro=False)
        puro = medir(*com_palma, forcar_puro=True)
        res.diz(f"    com palma: numpy={com_np and round(com_np.offset_seconds, 3)}s "
                f"puro={puro and round(puro.offset_seconds, 3)}s")
        res.checa(com_np is not None and puro is not None,
                  "audio com transiente produz resultado nos dois caminhos")
        if com_np and puro:
            res.checa(abs(com_np.offset_seconds - puro.offset_seconds) < 0.01,
                      f"numpy e Python puro concordam "
                      f"({com_np.offset_seconds:+.3f}s vs {puro.offset_seconds:+.3f}s)")
            # Os clipes de cadastro comecam com 2,4s de diferenca real.
            res.checa(abs(puro.offset_seconds - 2.4) < 0.1,
                      f"o deslocamento medido ({puro.offset_seconds:+.3f}s) bate com "
                      "os 2,4s de diferenca real entre os inicios")

    if planos[0] and planos[1]:
        ref, outro = planos[0][0], planos[1][0]
        res.checa(medir(ref, outro, False) is None and medir(ref, outro, True) is None,
                  "audio sem transiente: os dois caminhos RECUSAM responder, em vez "
                  "de devolver ruido com cara de resultado")


def teste_console_windows(res: Resultado) -> None:
    """A CLI nao pode quebrar num console que nao aceita UTF-8.

    O console do Windows usa cp1252, e cp1252 nao tem `→` — que aparece em todo
    lugar na saida deste app. Isso derrubava `dispositivos`, `organizar` e
    `calibrar` no meio da execucao, no executavel e tambem rodando do
    codigo-fonte. `PYTHONIOENCODING=cp1252` reproduz exatamente o mesmo erro no
    Linux, entao da para guardar a correcao sem precisar de um Windows.
    """
    import subprocess

    env = dict(os.environ, PYTHONIOENCODING="cp1252")
    pasta = os.path.join(MATERIAL, "04_dia_continuo")

    comandos = [(["dispositivos"], "dispositivos")]
    if os.path.isdir(pasta):
        comandos.append((["organizar", pasta, "--sem-exportar",
                          "--projeto", "cp1252"], "organizar"))

    for args, nome in comandos:
        proc = subprocess.run(
            [sys.executable, "-m", "timeline_sync", *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, cwd=RAIZ, timeout=600,
        )
        quebrou = "UnicodeEncodeError" in (proc.stderr or "")
        res.checa(proc.returncode == 0 and not quebrou,
                  f"`{nome}` sobrevive a um console cp1252 (rc={proc.returncode}"
                  + (", UnicodeEncodeError" if quebrou else "") + ")")


def teste_executavel(res: Resultado) -> None:
    """Confere o empacotamento, se ja houver um binario construido.

    Nao constroi nada: `build_exe.py` ja faz a verificacao completa. Aqui so
    garantimos que o que existe em dist/ nao ficou para tras.
    """
    import subprocess
    sufixo = ".exe" if os.name == "nt" else ""
    alvo = os.path.join(RAIZ, "dist", "onefile", "TimelineSync" + sufixo)
    if not os.path.isfile(alvo):
        res.diz("    (nenhum executavel em dist/ — rode `python build_exe.py`)")
        return
    proc = subprocess.run([alvo, "--versao"], capture_output=True, text=True,
                          timeout=120)
    res.checa(proc.returncode == 0,
              f"o executavel responde a --versao: "
              f"{(proc.stdout or proc.stderr).strip()}")


def teste_raiz_midia(res: Resultado) -> None:
    """Trocar a raiz da midia tem que reescrever os caminhos do XML."""
    pasta = os.path.join(MATERIAL, "04_dia_continuo")
    if not os.path.isdir(pasta):
        res.checa(False, "material do cenario 4 necessario")
        return
    session, project = sessao(pasta, "raiz", {
        "media_root_original": os.path.abspath(pasta),
        "media_root_override": "H:/Meu Drive/MINISSERIE",
    })
    destino = os.path.join(SAIDA, "_raiz_midia")
    if os.path.isdir(destino):
        shutil.rmtree(destino)
    exp = session.export(destino)
    texto = open(exp["files"][0], encoding="utf-8").read()
    res.checa("file://localhost/H:/Meu%20Drive/MINISSERIE" in texto,
              "os pathurl saem apontando para a raiz nova (sem relink manual)")
    res.checa(os.path.abspath(pasta) not in texto,
              "nenhum caminho antigo sobrou no XML")
    shutil.rmtree(destino, ignore_errors=True)


CENARIOS: List[Tuple[int, str, str, Callable]] = [
    (1, "01_fuso_errado", "Fuso errado (Sony 3h adiantada)", cenario_1),
    (2, "02_blocos_obvios", "Blocos obvios (pausa de 2h)", cenario_2),
    (3, "03_pausa_curta", "Pausa curta (~20 min)", cenario_3),
    (4, "04_dia_continuo", "Dia continuo (3h uniformes)", cenario_4),
    (5, "05_semana_completa", "Semana completa (5 dias)", cenario_5),
    (6, "06_fragmentacao", "Fragmentacao excessiva", cenario_6),
    (7, "07_virada_madrugada", "Virada de madrugada", cenario_7),
    (8, "08_metadado_ausente", "Metadado ausente", cenario_8),
]

EXTRAS: List[Tuple[str, Callable]] = [
    ("Leitura parcial em arquivo de 3 GB", teste_leitura_parcial),
    ("Modo manifesto de ingestao", teste_manifesto),
    ("Cache persistente (chave sem mtime)", teste_cache),
    ("Fechamento de tempo morto", teste_tempo_morto),
    ("Troca da raiz da midia no XML", teste_raiz_midia),
    ("Refino por audio (numpy vs Python puro)", teste_refino_audio),
    ("Console cp1252 (Windows)", teste_console_windows),
    ("Executavel empacotado", teste_executavel),
]


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--cenario", "-c", nargs="*", type=int)
    p.add_argument("--gerar", action="store_true", help="regera o material antes")
    p.add_argument("--escala", type=float, default=1.0)
    p.add_argument("--sem-extras", action="store_true")
    args = p.parse_args(argv)

    escolhidos = [c for c in CENARIOS if not args.cenario or c[0] in args.cenario]

    if args.gerar or not os.path.isdir(MATERIAL):
        numeros = [c[0] for c in escolhidos]
        gerar_testes.main(["--saida", MATERIAL, "--escala", str(args.escala),
                           "--cenario", *[str(n) for n in numeros]])
    faltando = [slug for _, slug, _, _ in escolhidos
                if not os.path.isdir(os.path.join(MATERIAL, slug))]
    if faltando:
        gerar_testes.main(["--saida", MATERIAL, "--escala", str(args.escala),
                           "--cenario",
                           *[str(c[0]) for c in escolhidos
                             if c[1] in faltando]])

    os.makedirs(SAIDA, exist_ok=True)
    resultados: List[Resultado] = []
    linhas: List[str] = []
    linhas.append("=" * 78)
    linhas.append("TIMELINE SYNC — VALIDACAO DOS CENARIOS")
    linhas.append(f"gerado em {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    linhas.append("=" * 78)

    for numero, slug, titulo, funcao in escolhidos:
        res = Resultado(numero, titulo)
        pasta = os.path.join(MATERIAL, slug)
        print(f"\n[{numero}] {titulo}")
        try:
            funcao(pasta, res)
        except Exception as exc:
            import traceback
            res.ok = False
            res.erros.append(f"{type(exc).__name__}: {exc}")
            res.linhas.append("    [FALHA] excecao: " + traceback.format_exc())
        resultados.append(res)
        for linha in res.linhas:
            print(linha)
        print(f"    => {'PASSOU' if res.ok else 'FALHOU'}")

    if not args.sem_extras and not args.cenario:
        for titulo, funcao in EXTRAS:
            res = Resultado(0, titulo)
            print(f"\n[extra] {titulo}")
            try:
                funcao(res)
            except Exception as exc:
                import traceback
                res.ok = False
                res.erros.append(f"{type(exc).__name__}: {exc}")
                res.linhas.append("    [FALHA] excecao: " + traceback.format_exc())
            resultados.append(res)
            for linha in res.linhas:
                print(linha)
            print(f"    => {'PASSOU' if res.ok else 'FALHOU'}")

    for res in resultados:
        linhas.append("")
        rotulo = f"CENARIO {res.numero}" if res.numero else "EXTRA"
        linhas.append(f"{rotulo}: {res.titulo}  =>  "
                      f"{'PASSOU' if res.ok else 'FALHOU'}")
        linhas.extend(res.linhas)

    passou = sum(1 for r in resultados if r.ok)
    linhas.append("")
    linhas.append("=" * 78)
    linhas.append(f"RESULTADO: {passou}/{len(resultados)} verificacoes de cenario "
                  "passaram")
    for res in resultados:
        if not res.ok:
            linhas.append(f"  FALHOU {res.titulo}: {'; '.join(res.erros)}")
    linhas.append("=" * 78)

    with open(LOG, "w", encoding="utf-8") as fh:
        fh.write("\n".join(linhas) + "\n")
    print(f"\n{passou}/{len(resultados)} passaram. Log completo em {LOG}")
    return 0 if passou == len(resultados) else 1


if __name__ == "__main__":
    raise SystemExit(main())
