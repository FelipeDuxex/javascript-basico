#!/usr/bin/env python3
"""Empacota o Timeline Sync num executavel com PyInstaller.

    python build_exe.py                 # onefile + onedir, e verifica os dois
    python build_exe.py --modo onefile  # so o arquivo unico
    python build_exe.py --sem-verificar # pula os testes de fumaca

Roda no sistema em que e executado: no Windows sai `.exe`, no Linux sai um
binario ELF, no macOS um Mach-O. **Nao existe cross-compile em PyInstaller** —
para gerar o `.exe` e preciso um Windows, e por isso existe o workflow
`.github/workflows/build-exe.yml`, que faz isso no CI a cada tag ou sob demanda.
Rodar este script no Linux serve para validar o empacotamento em si (recursos
embutidos, imports dinamicos, inicializacao) antes de gastar uma rodada de CI.

Dois formatos, de proposito:

* **onefile** — um arquivo so, o que a maioria das pessoas espera de um `.exe`.
  Custo: inicia mais devagar (descompacta num diretorio temporario a cada
  execucao) e e o formato que mais atrai falso-positivo de antivirus.
* **onedir** (distribuido como `.zip`) — pasta com o executavel e as DLLs ao
  lado. Inicia rapido e da menos problema com antivirus. E o plano B quando o
  onefile emperra.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import platform
import shutil
import subprocess
import sys
import zipfile
from typing import List, Optional, Sequence

RAIZ = os.path.dirname(os.path.abspath(__file__))
ENTRADA = os.path.join(RAIZ, "TimelineSync.py")
ICONE = os.path.join(RAIZ, "build", "timeline_sync.ico")
DIST = os.path.join(RAIZ, "dist")
TRABALHO = os.path.join(RAIZ, "build", "_pyinstaller")
NOME = "TimelineSync"

# Modulos pesados que o app nao usa. Sem isso o PyInstaller varre e embute
# coisas do ambiente de build que nao tem nada a ver com o programa.
EXCLUIR = [
    "tkinter", "PIL", "matplotlib", "pandas", "scipy", "IPython", "pytest",
    "setuptools", "pip", "wheel", "pyinstaller", "sqlite3.test", "test",
    "unittest", "pydoc_data", "lib2to3", "distutils",
]


def _sep() -> str:
    """Separador de `--add-data`: `;` no Windows, `:` no resto."""
    return ";" if os.name == "nt" else ":"


def _tem_modulo(nome: str) -> bool:
    try:
        __import__(nome)
        return True
    except ImportError:
        return False


def argumentos(modo: str) -> List[str]:
    # A origem precisa ser absoluta: o PyInstaller resolve caminhos relativos de
    # `--add-data` contra o `--specpath`, nao contra o diretorio atual.
    origem = os.path.join(RAIZ, "timeline_sync", "web", "static")
    destino = os.path.join("timeline_sync", "web", "static")
    args = [
        ENTRADA,
        "--name", NOME,
        "--console",              # o console mostra a URL, o progresso e os erros
        "--noconfirm",
        "--clean",
        "--distpath", os.path.join(DIST, modo),
        "--workpath", os.path.join(TRABALHO, modo),
        "--specpath", os.path.join(TRABALHO, "spec"),
        "--paths", RAIZ,
        "--add-data", f"{origem}{_sep()}{destino}",
    ]
    args.append("--onefile" if modo == "onefile" else "--onedir")

    if os.path.isfile(ICONE):
        args += ["--icon", ICONE]

    # O Windows nao tem base de fusos do sistema: sem `tzdata`, o zoneinfo falha
    # e o app cai no -03:00 fixo. Correto para Sao Paulo hoje (o Brasil acabou
    # com o horario de verao em 2019), mas quebraria qualquer outro fuso.
    if _tem_modulo("tzdata"):
        args += ["--collect-data", "tzdata", "--hidden-import", "tzdata"]

    # numpy fica FORA do executavel de proposito. A correlacao em Python puro
    # faz busca grosseira-e-fina e da a mesma resposta na mesma ordem de tempo
    # (medido: mesmo offset, mesma confianca), entao embutir numpy so somaria
    # ~30 MB e mais superficie para dar errado no empacotamento. Quem roda do
    # codigo-fonte e tem numpy instalado continua usando o caminho com numpy.
    args += ["--exclude-module", "numpy"]

    for modulo in EXCLUIR:
        args += ["--exclude-module", modulo]
    return args


def executavel(modo: str) -> str:
    sufixo = ".exe" if os.name == "nt" else ""
    if modo == "onefile":
        return os.path.join(DIST, modo, NOME + sufixo)
    return os.path.join(DIST, modo, NOME, NOME + sufixo)


def construir(modo: str) -> str:
    print(f"\n=== construindo ({modo}) ===")
    import PyInstaller.__main__
    PyInstaller.__main__.run(argumentos(modo))
    alvo = executavel(modo)
    if not os.path.isfile(alvo):
        raise SystemExit(f"ERRO: PyInstaller nao produziu {alvo}")
    tamanho = os.path.getsize(alvo)
    print(f"gerado: {alvo} ({tamanho / 1024 / 1024:.1f} MB)")
    return alvo


def _roda(alvo: str, args: Sequence[str], timeout: int = 300):
    return subprocess.run([alvo, *args], capture_output=True, text=True,
                          timeout=timeout, cwd=RAIZ)


def verificar(alvo: str) -> bool:
    """Testes de fumaca no binario ja empacotado.

    Empacotar "com sucesso" nao quer dizer nada: o modo de falha tipico do
    PyInstaller e gerar o executavel e ele morrer no primeiro import dinamico
    ou nao achar os arquivos da interface. Entao aqui o binario e exercitado
    de verdade — inclusive a leitura de metadado e a exportacao de XML.
    """
    ok = True

    print("\n--- verificando o executavel ---")
    proc = _roda(alvo, ["--versao"])
    print(f"  --versao: {proc.stdout.strip() or proc.stderr.strip()}")
    if proc.returncode != 0:
        print(f"  FALHA (rc={proc.returncode})\n{proc.stderr[:800]}")
        ok = False

    proc = _roda(alvo, ["dispositivos"])
    if proc.returncode != 0:
        print(f"  FALHA em `dispositivos` (rc={proc.returncode})\n{proc.stderr[:800]}")
        ok = False
    else:
        print("  `dispositivos`: OK")

    proc = _roda(alvo, ["cache"])
    if proc.returncode != 0:
        print(f"  FALHA em `cache` (rc={proc.returncode})\n{proc.stderr[:800]}")
        ok = False
    else:
        print(f"  `cache`: {proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else 'OK'}")

    material = os.path.join(RAIZ, "material_teste", "02_blocos_obvios")
    if os.path.isdir(material):
        saida = os.path.join(TRABALHO, "verificacao")
        shutil.rmtree(saida, ignore_errors=True)
        proc = _roda(alvo, ["organizar", material, "--saida", saida,
                            "--projeto", "verificacao-build"], timeout=600)
        if proc.returncode != 0:
            print(f"  FALHA ao organizar (rc={proc.returncode})\n{proc.stderr[:1200]}")
            ok = False
        else:
            xmls = [f for f in os.listdir(saida)] if os.path.isdir(saida) else []
            linha = next((l for l in proc.stdout.splitlines()
                          if "arquivos lidos" in l), "")
            print(f"  `organizar`: {linha.strip()}")
            print(f"  saida: {len(xmls)} arquivo(s) — {', '.join(sorted(xmls))}")
            if not any(f.endswith(".xml") for f in xmls):
                print("  FALHA: nenhum XML foi gerado")
                ok = False
    else:
        print("  (pulando o teste de organizar: material_teste/ nao existe —"
              " rode `python gerar_testes.py` para uma verificacao completa)")

    # A interface tem que subir e servir os estaticos de dentro do pacote.
    ok = verificar_interface(alvo) and ok
    return ok


def verificar_interface(alvo: str) -> bool:
    """Sobe o servidor a partir do binario e busca a pagina e os estaticos."""
    import time
    import urllib.error
    import urllib.request

    from timeline_sync.launcher import porta_livre
    porta = porta_livre(8790)
    if porta is None:
        print("  (sem porta livre para testar a interface)")
        return True

    env = dict(os.environ, TIMELINE_SYNC_TESTE="1")
    proc = subprocess.Popen([alvo, "web", "--porta", str(porta),
                             "--sem-navegador"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, cwd=RAIZ, env=env)
    try:
        base = f"http://127.0.0.1:{porta}"
        conteudo = {}
        for _ in range(40):
            try:
                for rota in ("/", "/static/style.css", "/static/app.js",
                             "/api/estado"):
                    with urllib.request.urlopen(base + rota, timeout=3) as r:
                        conteudo[rota] = len(r.read())
                break
            except (urllib.error.URLError, OSError):
                if proc.poll() is not None:
                    saida = proc.stdout.read() if proc.stdout else ""
                    print(f"  FALHA: o servidor morreu.\n{saida[:1200]}")
                    return False
                time.sleep(0.25)
        else:
            print("  FALHA: a interface nao respondeu a tempo")
            return False

        faltando = [r for r, n in conteudo.items() if n <= 0]
        if faltando or len(conteudo) < 4:
            print(f"  FALHA: rotas vazias ou ausentes: {faltando or conteudo}")
            return False
        print("  interface: " + ", ".join(f"{r} {n}B" for r, n in conteudo.items()))
        return True
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def empacotar_zip(modo: str) -> Optional[str]:
    """Compacta a build onedir num zip pronto para distribuir."""
    if modo != "onedir":
        return None
    pasta = os.path.join(DIST, modo, NOME)
    if not os.path.isdir(pasta):
        return None
    sistema = {"Windows": "windows", "Darwin": "macos"}.get(platform.system(), "linux")
    destino = os.path.join(DIST, f"{NOME}-{sistema}-pasta.zip")
    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for raiz, _dirs, arquivos in os.walk(pasta):
            for arquivo in arquivos:
                completo = os.path.join(raiz, arquivo)
                z.write(completo, os.path.relpath(completo, os.path.dirname(pasta)))
        leiame = os.path.join(RAIZ, "build", "LEIA-ME-EXECUTAVEL.txt")
        if os.path.isfile(leiame):
            z.write(leiame, os.path.join(NOME, "LEIA-ME.txt"))
    print(f"zip: {destino} ({os.path.getsize(destino) / 1024 / 1024:.1f} MB)")
    return destino


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--modo", choices=["onefile", "onedir", "ambos"], default="ambos")
    p.add_argument("--sem-verificar", action="store_true")
    p.add_argument("--sem-icone", action="store_true")
    args = p.parse_args(argv)

    if importlib.util.find_spec("PyInstaller") is None:
        print("PyInstaller nao instalado. Rode:  pip install pyinstaller",
              file=sys.stderr)
        return 1

    if not args.sem_icone:
        from build.gerar_icone import escreve_ico
        escreve_ico(ICONE)
        print(f"icone: {ICONE}")

    modos = ["onefile", "onedir"] if args.modo == "ambos" else [args.modo]
    tudo_ok = True
    gerados: List[str] = []
    for modo in modos:
        alvo = construir(modo)
        gerados.append(alvo)
        if not args.sem_verificar:
            if not verificar(alvo):
                tudo_ok = False
                print(f"  => {modo}: VERIFICACAO FALHOU")
            else:
                print(f"  => {modo}: OK")
        empacotar_zip(modo)

    print("\n=== resultado ===")
    print(f"sistema: {platform.system()} {platform.machine()} | "
          f"python {platform.python_version()}")
    for alvo in gerados:
        print(f"  {alvo} ({os.path.getsize(alvo) / 1024 / 1024:.1f} MB)")
    if os.name != "nt":
        print("\nEste binario e para o sistema atual, nao um .exe do Windows.")
        print("O .exe sai do workflow .github/workflows/build-exe.yml (roda no CI).")
    return 0 if tudo_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
