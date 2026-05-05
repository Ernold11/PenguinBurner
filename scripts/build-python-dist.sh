#!/usr/bin/env bash

set -euo pipefail

outdir="${1:-dist/python}"

rm -rf "$outdir"
mkdir -p "$outdir"

python3 -m pip install --upgrade build twine
python3 -m build --sdist --wheel --outdir "$outdir"
python3 -m twine check "$outdir"/*
