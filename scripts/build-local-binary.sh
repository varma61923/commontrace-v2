#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# Build-time dependency only: python3 -m pip install pyinstaller
python3 -m PyInstaller --noconfirm --clean --onefile --name unimem-local \
  --specpath build --collect-all unimem --collect-all yaml \
  --exclude-module torch --exclude-module sentence_transformers \
  --exclude-module transformers --exclude-module gliner unimem/__main__.py
./dist/unimem-local --version
./dist/unimem-local evolve recipes >/dev/null
printf 'Standalone local binary: dist/unimem-local\n'
