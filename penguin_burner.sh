#!/usr/bin/env bash

set -euo pipefail

SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
    SCRIPT_DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
    SOURCE="$(readlink "$SOURCE")"
    case "$SOURCE" in
        /*) ;;
        *) SOURCE="$SCRIPT_DIR/$SOURCE" ;;
    esac
done
SCRIPT_DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || true)}"

if [ -z "$PYTHON_BIN" ]; then
    echo "error: python3 not found in PATH" >&2
    exit 1
fi

MAIN_SCRIPT="$SCRIPT_DIR/penguin_burner.py"
if [ ! -f "$MAIN_SCRIPT" ]; then
    echo "error: penguin_burner.py not found next to $0" >&2
    exit 1
fi

exec "$PYTHON_BIN" "$MAIN_SCRIPT" "$@"
