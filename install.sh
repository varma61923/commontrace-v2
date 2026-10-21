#!/usr/bin/env bash

set -euo pipefail

DEST="${HOME}/.unimem"
INSTALL_DEPS=true
BUILD_INDEX=true
IN_PLACE=false
PYTHON="${PYTHON:-python3}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dest)
      [[ $# -ge 2 ]] || { echo "[ERROR] --dest requires a path argument" >&2; exit 1; }
      DEST="$2"; shift 2 ;;
    --dest=*)     DEST="${1#--dest=}"; shift ;;
    --in-place)   IN_PLACE=true; shift ;;
    --no-deps)    INSTALL_DEPS=false; shift ;;
    --no-index)   BUILD_INDEX=false; shift ;;
    --python)
      [[ $# -ge 2 ]] || { echo "[ERROR] --python requires a binary argument" >&2; exit 1; }
      PYTHON="$2"; shift 2 ;;
    --python=*)   PYTHON="${1#--python=}"; shift ;;
    --help|-h)
      cat <<'USAGE'
install.sh — unimem setup script

Usage:
  ./install.sh                  # install to ~/.unimem (default)
  ./install.sh --dest /my/path  # install to a custom path
  ./install.sh --in-place       # skip copy, install deps for repo checkout
  ./install.sh --no-deps        # skip pip install (deps already present)
  ./install.sh --no-index       # skip building the attention index
  ./install.sh --help

After install, scripts auto-detect their root from their own location.
Only set UNIMEM_ROOT if you need to override the auto-detected path.
USAGE
      exit 0 ;;
    *) echo "[ERROR] Unknown argument: $1" >&2; exit 1 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

if [[ "${IN_PLACE}" == "true" ]]; then
  DEST="${SCRIPT_DIR}"
fi

DEST_CANONICAL=$(mkdir -p "${DEST}" 2>/dev/null && cd "${DEST}" 2>/dev/null && pwd -P || echo "${DEST}")
if [[ "${DEST_CANONICAL}" == "/" || "${DEST_CANONICAL}" == "/etc" || "${DEST_CANONICAL}" == "/bin" || "${DEST_CANONICAL}" == "/sbin" || "${DEST_CANONICAL}" == "/usr" || "${DEST_CANONICAL}" == "/var" || "${DEST_CANONICAL}" == "/home" || -z "${DEST}" ]]; then
  echo "[ERROR] Refusing to install into system root or protected directory: ${DEST}" >&2
  exit 1
fi

if [[ "${IN_PLACE}" != "true" && ( "${DEST_CANONICAL}" == "${SCRIPT_DIR}" || "${DEST_CANONICAL}" == "${SCRIPT_DIR}/"* ) ]]; then
  echo "[ERROR] Copy destination must be outside the source tree; use --in-place for the checkout." >&2
  exit 1
fi

if ! command -v "${PYTHON}" &>/dev/null; then
  echo "[ERROR] Python not found: ${PYTHON}" >&2
  echo "        Install Python 3.10+ or set --python=/path/to/python3" >&2
  exit 1
fi

PY_VERSION=$("${PYTHON}" --version 2>&1)

echo ""
echo "=== unimem installer ==="
echo "  Source  : ${SCRIPT_DIR}"
echo "  Dest    : ${DEST}"
echo "  Python  : ${PYTHON} (${PY_VERSION})"
echo ""

if [[ "${IN_PLACE}" == "true" ]]; then
  echo "[1/4] In-place mode — skipping file copy."
else
  echo "[1/4] Copying skill files to ${DEST} ..."
  mkdir -p "${DEST}"

  if command -v rsync &>/dev/null; then
    rsync -a --no-times \
      --exclude='.git' \
      --exclude='sdk/rust/target' \
      --exclude='sdk/jvm/target' \
      --exclude='sdk/generated' \
      --exclude='build' \
      --exclude='dist' \
      --exclude='.terraform' \
      --exclude='.gradle' \
      --exclude='.intellijPlatform' \
      --exclude='.mypy_cache' \
      --exclude='.pytest_cache' \
      --exclude='.ruff_cache' \
      --exclude='node_modules' \
      --exclude='target' \
      --exclude='*.tfstate' \
      --exclude='*.tfstate.*' \
      --exclude='*.tfplan' \
      --exclude='*.tfvars' \
      --exclude='__pycache__' \
      --exclude='*.pyc' \
      --exclude='*.pyo' \
      --exclude='memory/attention/index.npz' \
      --exclude='memory/benchmark_reports/' \
      --exclude='.venv' \
      --exclude='venv' \
      --include='.env.example' \
      --include='*/.env.example' \
      --exclude='.env' \
      --exclude='.env.*' \
      "${SCRIPT_DIR}/" "${DEST}/"
  else
    # Exclude build outputs before copying; generated SDK caches may contain
    # unreadable compiler locks and are never part of an installed skill.
    tar -C "${SCRIPT_DIR}" --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' \
      --exclude='node_modules' --exclude='.venv' --exclude='.pytest_cache' --exclude='.ruff_cache' \
      --exclude='./sdk/rust/target' --exclude='./sdk/jvm/target' --exclude='./sdk/generated' \
      --exclude='build' --exclude='dist' --exclude='target' --exclude='.env' --exclude='.env.*' \
      --exclude='.terraform' --exclude='.gradle' --exclude='.intellijPlatform' --exclude='.mypy_cache' \
      --exclude='*.tfstate' --exclude='*.tfstate.*' --exclude='*.tfplan' --exclude='*.tfvars' \
      --exclude='./memory/attention/index.npz' --exclude='./memory/benchmark_reports' -cf - . | tar -C "${DEST}" -xf -
    # Exclusions govern the incoming archive. Existing destination credentials,
    # Git metadata and generated files belong to its owner and must be retained.
  fi

  echo "      Done."
fi

if [[ "${INSTALL_DEPS}" == "true" ]]; then
  echo "[2/4] Installing Python dependencies from requirements.txt ..."
  REQS="${SCRIPT_DIR}/requirements.txt"
  if [[ ! -f "${REQS}" ]]; then
    echo "      [WARN] requirements.txt not found at ${REQS} — skipping."
  else
    "${PYTHON}" -m pip install --quiet -r "${REQS}" \
      && echo "      Done." \
      || { echo "      [WARN] pip install failed. You may need to install manually:"; \
           echo "             ${PYTHON} -m pip install -r ${REQS}"; }
  fi
  echo "      Installing the unimem package (registers the 'unimem' command) ..."
  "${PYTHON}" -m pip install --quiet -e "${DEST}" \
    && echo "      Done." \
    || { echo "      [WARN] pip install -e failed. You may need to install manually:"; \
         echo "             ${PYTHON} -m pip install -e ${DEST}"; }
else
  echo "[2/4] Skipping dependency install (--no-deps)."
fi

echo "[3/4] Verifying dependencies ..."
MISSING=""
for pkg in numpy yaml sentence_transformers; do
  if ! "${PYTHON}" -c "import ${pkg}" 2>/dev/null; then
    MISSING="${MISSING}${MISSING:+ }${pkg}"
  fi
done
if [[ -n "${MISSING}" ]]; then
  echo "      [WARN] Missing packages: ${MISSING}"
  echo "      Run:  ${PYTHON} -m pip install -r ${DEST}/requirements.txt"
else
  echo "      All core packages found."
fi

if [[ "${BUILD_INDEX}" == "true" ]]; then
  echo "[4/4] Building attention index ..."
  LESSONS_DIR="${DEST}/memory/lessons"
  lesson_count=0
  if [[ -d "${LESSONS_DIR}" ]]; then
    lesson_count=$(find "${LESSONS_DIR}" -name "lesson_*.md" ! -name "lesson_template.md" | wc -l | tr -d ' ')
  fi
  if [[ "${lesson_count}" -eq 0 ]]; then
    echo "      No active lessons found — skipping index build."
    echo "      (Re-run later: unimem index)"
  else
    if [[ -f "${DEST}/unimem/reference/build_index.py" ]]; then
      UNIMEM_ROOT="${DEST}" "${PYTHON}" "${DEST}/unimem/reference/build_index.py" \
        && echo "      Index built." \
        || echo "      [WARN] Index build failed (sentence-transformers may not be installed yet)."
    else
      UNIMEM_ROOT="${DEST}" "${PYTHON}" -m unimem.cli index \
        && echo "      Index built." \
        || echo "      [WARN] Index build failed (sentence-transformers may not be installed yet)."
    fi
  fi
else
  echo "[4/4] Skipping attention index build (--no-index)."
fi

echo ""
echo "=== Install complete ==="
echo ""
echo "  Skill root : ${DEST}"
echo ""
echo "  Scripts auto-detect their root from their own file location."
echo "  UNIMEM_ROOT env var is only needed to override the auto-detected path."
echo ""
echo "  Quick start:"
echo "    # Run benchmark"
echo "    unimem bench            # memory health"
echo "    unimem bench --pilot    # business-outcome metrics"
echo ""
echo "    # Query memory"
echo "    unimem query \"my task description\""
echo ""
echo "    # Rebuild attention index"
echo "    unimem index"
echo ""
