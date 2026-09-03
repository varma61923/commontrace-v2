#!/usr/bin/env bash
# install.sh — commontrace setup script
#
# Usage:
#   ./install.sh                  # install to ~/.commontrace (default)
#   ./install.sh --dest /my/path  # install to a custom path
#   ./install.sh --in-place       # skip copy, install deps for repo checkout
#   ./install.sh --no-deps        # skip pip install (deps already present)
#   ./install.sh --no-index       # skip building the attention index
#   ./install.sh --help
#
# After install, scripts auto-detect their root from their own location.
# Only set COMMONTRACE_ROOT if you need to override the auto-detected path.

set -euo pipefail

# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #
DEST="${HOME}/.commontrace"
INSTALL_DEPS=true
BUILD_INDEX=true
IN_PLACE=false
PYTHON="${PYTHON:-python3}"

# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dest)
      # Under `set -u`, referencing $2 when the flag was the last argument
      # (`./install.sh --dest` with nothing after it) is an unbound-variable
      # error that aborts with a bash internal message instead of this
      # script's own actionable one.
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
      sed -n '2,13p' "$0"
      exit 0 ;;
    *) echo "[ERROR] Unknown argument: $1" >&2; exit 1 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# In-place mode: skip copy, use the repo checkout directly
if [[ "${IN_PLACE}" == "true" ]]; then
  DEST="${SCRIPT_DIR}"
fi

# Verify Python is available
if ! command -v "${PYTHON}" &>/dev/null; then
  echo "[ERROR] Python not found: ${PYTHON}" >&2
  echo "        Install Python 3.10+ or set --python=/path/to/python3" >&2
  exit 1
fi

PY_VERSION=$("${PYTHON}" --version 2>&1)

echo ""
echo "=== commontrace installer ==="
echo "  Source  : ${SCRIPT_DIR}"
echo "  Dest    : ${DEST}"
echo "  Python  : ${PYTHON} (${PY_VERSION})"
echo ""

# --------------------------------------------------------------------------- #
# 1. Copy skill files to destination (skip if in-place)
# --------------------------------------------------------------------------- #
if [[ "${IN_PLACE}" == "true" ]]; then
  echo "[1/4] In-place mode — skipping file copy."
else
  echo "[1/4] Copying skill files to ${DEST} ..."
  mkdir -p "${DEST}"

  if command -v rsync &>/dev/null; then
    # --no-times: `-a` (archive mode) implies `-t`, which preserves each
    # file's SOURCE mtime at the destination. build_index.py's staleness
    # check compares memory/lessons/*.md mtimes against index.npz's own
    # mtime to decide whether a rebuild is needed -- with source mtimes
    # preserved, a lesson file copied here (e.g. this repo's own shipped
    # example lessons) can land with an mtime OLDER than an existing
    # index.npz at DEST from a previous install, even though its content
    # just changed at this destination for the first time. The index then
    # reports "up to date" and silently serves stale embeddings. Every
    # copied file gets DEST's actual copy time instead, which is what the
    # staleness check needs it to mean.
    rsync -a --no-times \
      --exclude='.git' \
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
    # Fallback: plain copy
    cp -r "${SCRIPT_DIR}/." "${DEST}/"
    # Clean up generated files in the copy
    rm -f "${DEST}/memory/attention/index.npz" 2>/dev/null || true
    rm -rf "${DEST}/.git" 2>/dev/null || true
    find "${DEST}" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
    find "${DEST}" -name '*.pyc' -delete 2>/dev/null || true
    # .env* files can hold live database passwords / API keys (see
    # hub/.env.example's own warning that hub/.env must stay gitignored
    # once it holds real values) -- the rsync branch above excludes them
    # up front, but `cp -r` copies everything unconditionally first, so
    # this fallback path must clean them up explicitly too rather than
    # silently installing credentials into ${DEST}. .env.example is a
    # committed, secret-free template and is deliberately spared.
    # `-exec ... +`, not `| xargs -r`: BSD/macOS xargs has no `-r`
    # (--no-run-if-empty) and exits with "illegal option -- r" under `set
    # -e`, aborting the installer on exactly the platform this fallback
    # branch's own comments say to target. `-exec rm -f {} +` needs no
    # such flag (find simply runs nothing when nothing matches) and, unlike
    # piping through xargs, never splits an unquoted path on whitespace --
    # so a DEST containing a space cannot make this delete an unrelated
    # path fragment. `\( -name '.env' -o -name '.env.*' \)` groups the OR
    # explicitly rather than relying on find's default precedence.
    find "${DEST}" \( -name '.env' -o -name '.env.*' \) ! -name '.env.example' -exec rm -f {} + 2>/dev/null || true
  fi

  echo "      Done."
fi

# --------------------------------------------------------------------------- #
# 2. Install Python dependencies
# --------------------------------------------------------------------------- #
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
  # requirements.txt alone only satisfies the commontrace *package's*
  # imports -- it never registers the `commontrace` console script
  # ([project.scripts] in pyproject.toml), because that registration only
  # happens when pip installs the package itself. Without this, every
  # command this script's own Quick Start / summary output tells the user
  # to run next (`commontrace bench`, `commontrace capture`, ...) fails
  # with "command not found", even though setup otherwise "succeeded".
  # `-e`, not a plain install: DEST is either the repo checkout itself
  # (--in-place) or a copy of it, and package-data (schemas/*.json,
  # reference/*.py) plus any future edits at DEST should keep working
  # exactly like a checkout, not get frozen into a site-packages copy.
  echo "      Installing the commontrace package (registers the 'commontrace' command) ..."
  "${PYTHON}" -m pip install --quiet -e "${DEST}" \
    && echo "      Done." \
    || { echo "      [WARN] pip install -e failed. You may need to install manually:"; \
         echo "             ${PYTHON} -m pip install -e ${DEST}"; }
else
  echo "[2/4] Skipping dependency install (--no-deps)."
fi

# --------------------------------------------------------------------------- #
# 3. Verify core dependencies
# --------------------------------------------------------------------------- #
echo "[3/4] Verifying dependencies ..."
# A space-separated string, not an array. Under `set -u`, bash < 4.4 -- which
# is what stock macOS still ships (3.2) -- treats ${#ARR[@]} on an empty array
# as an unbound variable and aborts. That fires only on the SUCCESS path, so
# the installer would die precisely when every dependency was already present.
# A string has no such edge case and this is only ever a warning list.
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

# --------------------------------------------------------------------------- #
# 4. Build attention index
# --------------------------------------------------------------------------- #
if [[ "${BUILD_INDEX}" == "true" ]]; then
  echo "[4/4] Building attention index ..."
  LESSONS_DIR="${DEST}/memory/lessons"
  lesson_count=$(find "${LESSONS_DIR}" -name "lesson_*.md" ! -name "lesson_template.md" 2>/dev/null | wc -l | tr -d ' ')
  if [[ "${lesson_count}" -eq 0 ]]; then
    echo "      No active lessons found — skipping index build."
    echo "      (Re-run later: ${PYTHON} ${DEST}/memory/attention/build_index.py)"
  else
    COMMONTRACE_ROOT="${DEST}" "${PYTHON}" "${DEST}/memory/attention/build_index.py" \
      && echo "      Index built." \
      || echo "      [WARN] Index build failed (sentence-transformers may not be installed yet)."
  fi
else
  echo "[4/4] Skipping attention index build (--no-index)."
fi

# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #
echo ""
echo "=== Install complete ==="
echo ""
echo "  Skill root : ${DEST}"
echo ""
echo "  Scripts auto-detect their root from their own file location."
echo "  COMMONTRACE_ROOT env var is only needed to override the auto-detected path."
echo ""
echo "  Quick start:"
echo "    # Run benchmark"
echo "    commontrace bench            # memory health"
echo "    commontrace bench --pilot    # business-outcome metrics"
echo ""
echo "    # Query memory"
echo "    ${PYTHON} ${DEST}/memory/attention/query.py \"my task description\""
echo ""
echo "    # Rebuild attention index"
echo "    ${PYTHON} ${DEST}/memory/attention/build_index.py"
echo ""
