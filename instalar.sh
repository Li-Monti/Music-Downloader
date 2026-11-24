#!/usr/bin/env bash
set -euo pipefail
APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python3 -c 'import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] < (3, 16) else "Se requiere Python 3.11 a 3.15.")'
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install --upgrade --requirement "$APP_DIR/requirements.txt"
echo 'Instalación lista. Ejecutá ./mp3 para pegar una canción o playlist.'
