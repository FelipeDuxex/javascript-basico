"""Ponto de entrada do executavel.

Duplo-clique tem expectativas diferentes de uma linha de comando:

* **sem argumentos** → abre a interface no navegador e pronto. Ninguem que
  clicou num `.exe` quer digitar `web` depois;
* **porta ocupada** → tenta as proximas, em vez de morrer com um traceback;
* **erro** → a janela do console NAO pode sumir antes de a pessoa ler o que
  aconteceu. Sem isso, um erro de inicializacao vira "o programa nao abre";
* **com argumentos** → funciona igual a CLI (`TimelineSync.exe organizar ...`).
"""

from __future__ import annotations

import socket
import sys
import traceback
from typing import List, Optional, Sequence

from . import APP_NAME, __version__
from .runtime import app_dir, configurar_console, is_frozen, tool_status

PORTA_PADRAO = 8730
TENTATIVAS_DE_PORTA = 20


def porta_livre(inicial: int = PORTA_PADRAO,
                tentativas: int = TENTATIVAS_DE_PORTA) -> Optional[int]:
    """Primeira porta livre a partir de `inicial`, em 127.0.0.1."""
    for porta in range(inicial, inicial + tentativas):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", porta))
                return porta
            except OSError:
                continue
    return None


def _aviso_ferramentas() -> List[str]:
    """O que da e o que nao da para fazer com o que esta instalado."""
    status = tool_status()
    linhas: List[str] = []
    if status["tem_ffmpeg"] and status["tem_ffprobe"]:
        linhas.append(f"ffmpeg encontrado: {status['ffmpeg']}")
        return linhas
    linhas.append("ffmpeg/ffprobe NAO encontrados — e isso esta OK.")
    linhas.append("  O app le MP4, MOV e WAV com parser proprio e nao precisa deles")
    linhas.append("  para organizar e exportar. Ficam indisponiveis apenas:")
    linhas.append("    - refino de sync por audio (o botao 'Refinar sync por audio')")
    linhas.append("    - o refino por palma na calibracao (o offset em horas continua)")
    linhas.append("    - gerar_testes.py (material sintetico)")
    linhas.append("  Para habilitar: baixe o ffmpeg e coloque ffmpeg.exe e")
    linhas.append(f"  ffprobe.exe em {app_dir()}")
    return linhas


def _pausa_se_duplo_clique(houve_erro: bool = False) -> None:
    """Segura a janela do console para a mensagem poder ser lida.

    So faz sentido no executavel: rodando pelo terminal, a janela ja fica.
    """
    if not is_frozen():
        return
    if not (houve_erro or sys.stdout.isatty()):
        return
    try:
        input("\nPressione Enter para fechar. ")
    except (EOFError, KeyboardInterrupt):
        pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    configurar_console()
    args = list(sys.argv[1:] if argv is None else argv)

    # Com argumentos, comporta-se exatamente como a CLI.
    if args:
        from .cli import main as cli_main
        try:
            return cli_main(args)
        except KeyboardInterrupt:
            print("\ncancelado.")
            return 130
        except Exception:
            traceback.print_exc()
            _pausa_se_duplo_clique(houve_erro=True)
            return 1

    # Sem argumentos: modo "abriu o programa".
    print(f"{APP_NAME} {__version__}")
    print("=" * 52)
    for linha in _aviso_ferramentas():
        print(linha)
    print("=" * 52)

    porta = porta_livre()
    if porta is None:
        print(f"ERRO: nenhuma porta livre entre {PORTA_PADRAO} e "
              f"{PORTA_PADRAO + TENTATIVAS_DE_PORTA - 1}.")
        print("Feche outras instancias do Timeline Sync e tente de novo.")
        _pausa_se_duplo_clique(houve_erro=True)
        return 1
    if porta != PORTA_PADRAO:
        print(f"(porta {PORTA_PADRAO} ocupada — usando {porta})")

    try:
        from .web import serve
        return serve(port=porta, project="projeto", open_browser=True)
    except Exception:
        print("\nERRO ao iniciar a interface:\n")
        traceback.print_exc()
        _pausa_se_duplo_clique(houve_erro=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
