#!/usr/bin/env bash
# Create a reproducible development environment for `portable`.
#
#   scripts/bootstrap.sh [--no-native]
#
# Idempotent: safe to re-run. The Windows equivalent is scripts/bootstrap.ps1
# and the two must stay in step -- the owner works on Windows and CI runs both.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"
VENV="${VENV:-$ROOT/.venv}"
BUILD_NATIVE="ON"

for arg in "$@"; do
  case "$arg" in
    --no-native) BUILD_NATIVE="OFF" ;;
    -h|--help)
      sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "error: $PY not found. portable needs Python 3.11 or newer." >&2
  exit 1
fi

"$PY" - <<'PYCHECK'
import sys
if sys.version_info < (3, 11):
    sys.exit(f"error: Python 3.11+ required, found {sys.version.split()[0]}")
PYCHECK

echo "==> virtualenv at $VENV"
[ -d "$VENV" ] || "$PY" -m venv "$VENV"
BIN="$VENV/bin"

echo "==> dependencies (pinned by constraints.txt)"
"$BIN/python" -m pip install --quiet --upgrade pip
"$BIN/pip" install --quiet -r requirements-dev.txt -c constraints.txt

if [ "$BUILD_NATIVE" = "ON" ] && ! command -v cmake >/dev/null 2>&1; then
  echo "==> cmake not found; installing without the native extension."
  echo "    This is a complete, correct install -- the pure-Python path is the"
  echo "    reference implementation, not a degraded mode (ADR 0008)."
  BUILD_NATIVE="OFF"
fi

echo "==> installing portable (editable, native=$BUILD_NATIVE)"
PORTABLE_BUILD_NATIVE="$BUILD_NATIVE" "$BIN/pip" install --quiet -e . --no-build-isolation --no-deps

echo "==> verifying"
"$BIN/python" - <<'PYVERIFY'
from portable_core import __version__, native
print(f"    portable_core {__version__}, native implementation: {native.implementation()}")
PYVERIFY
"$BIN/python" -m portable_core.lint all >/dev/null && echo "    lint rules: clean"

cat <<MSG

Ready. Activate with:

    source $VENV/bin/activate

Then:

    pt --help                  the portfolio tool
    make check                 everything CI runs
    make test-fast             the fast unit subset

A worked example is in examples/walkthrough.md.
MSG
