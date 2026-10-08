#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# Build-time dependency only: python3 -m pip install pyinstaller
python3 -m PyInstaller --noconfirm --clean --onefile --name commontrace-local \
  --specpath build --collect-all commontrace --collect-all yaml commontrace/__main__.py
./dist/commontrace-local --version
./dist/commontrace-local evolve recipes >/dev/null
printf 'Standalone local binary: dist/commontrace-local\n'
