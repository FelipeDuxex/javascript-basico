#!/usr/bin/env python3
"""Ponto de entrada do Timeline Sync.

Serve para os dois usos:

    python TimelineSync.py                 # abre a interface no navegador
    python TimelineSync.py organizar ...   # linha de comando

E e tambem o script que o PyInstaller empacota para virar `TimelineSync.exe`.
"""

import sys

from timeline_sync.launcher import main

if __name__ == "__main__":
    sys.exit(main())
