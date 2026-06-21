#!/usr/bin/env bash

set -euo pipefail

cd "$(dirname "$0")/.."

PYTHONPATH="${PYTHONPATH:-.}"
export PYTHONPATH
report_dir="${PB_STATIC_REPORT_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/pb-static.XXXXXX")}"

python_paths=(
    auto_uv
    cli
    common
    curve_editors
    drivers
    integrations
    overlay
    profiles
    runtime
    ui
    penguin_burner.py
)

count_paths=(
    auto_uv
    cli
    common
    curve_editors
    drivers
    integrations
    overlay
    profiles
    runtime
    ui
    penguin_burner.py
)

require_tool() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "missing required static-analysis tool: $1" >&2
        exit 2
    fi
}

run() {
    echo
    echo "==> $*"
    "$@"
}

require_tool rg
require_tool python3
require_tool ruff
require_tool vulture
require_tool pyright
require_tool scc
require_tool cloc
require_tool tokei

echo "PenguinBurner feature static-analysis routine"
echo "reports: $report_dir"

run git diff --check

echo
echo "==> stale moved-package import scan"
if rg -n \
    '^(from|import) (auto_oc|stability|initial_check|runtime_support|runtime_gpu_control|runtime_fan_control|runtime_stability_test|nvidia_driver|saved_uv_profiles|saved_profile_verification|latency_telemetry|manual_uv_curve_editor|manual_fan_curve_editor|afterburner|lact)(\b|\.)' \
    --glob '*.py'
then
    echo "stale moved-package imports found" >&2
    exit 1
fi

echo
echo "==> package-root facade import scan"
if rg -n \
    '^from (auto_uv\.stability\.q2rtx|runtime\.gpu_control|profiles\.uv) import|^import (auto_uv\.stability\.q2rtx|runtime\.gpu_control|profiles\.uv)(\s|$| as)' \
    --glob '*.py'
then
    echo "package-root facade imports found; import concrete modules instead" >&2
    exit 1
fi

run python3 -m compileall -q "${python_paths[@]}"

run python3 - <<'PY'
from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
modules = list(metadata["tool"]["setuptools"].get("py-modules", ()))
packages = list(metadata["tool"]["setuptools"].get("packages", ()))
for name in [*modules, *packages]:
    importlib.import_module(name)
print(f"imported {len(modules) + len(packages)} packaged modules/packages")
PY

run ruff check "${python_paths[@]}"

run vulture "${python_paths[@]}" --min-confidence "${VULTURE_MIN_CONFIDENCE:-80}"

echo
echo "==> pyright ${python_paths[*]} (advisory until the repo has a clean baseline)"
pyright_report="$report_dir/pyright.txt"
if ! pyright "${python_paths[@]}" >"$pyright_report" 2>&1; then
    if [ "${PB_STATIC_STRICT_PYRIGHT:-0}" = "1" ]; then
        cat "$pyright_report"
        echo "pyright failed and PB_STATIC_STRICT_PYRIGHT=1" >&2
        exit 1
    fi
    tail -80 "$pyright_report"
    echo "pyright reported existing type issues; review touched-file output."
    echo "full pyright report: $pyright_report"
else
    cat "$pyright_report"
fi

run scc --no-cocomo \
    --exclude-dir .git,__pycache__,build,dist,tests,third_party,reverse,nvuv-play,specs \
    "${count_paths[@]}"

run cloc \
    --exclude-dir=.git,__pycache__,build,dist,tests,third_party,reverse,nvuv-play,specs \
    "${count_paths[@]}"

run tokei \
    -e '*/__pycache__/*' \
    -e '*/build/*' \
    -e '*/dist/*' \
    -e '*/tests/*' \
    -e '*/third_party/*' \
    -e '*/reverse/*' \
    -e '*/nvuv-play/*' \
    -e '*/specs/*' \
    "${count_paths[@]}"

echo
echo "feature static-analysis routine complete"
