#!/usr/bin/env bash

set -euo pipefail

outdir="${1:-dist/python}"

rm -rf "$outdir"
mkdir -p "$outdir"

python3 -m pip install --upgrade build cibuildwheel twine
PENGUIN_BURNER_REQUIRE_NATIVE_LAYER=1 \
    python3 -m build --sdist --outdir "$outdir"
PENGUIN_BURNER_REQUIRE_NATIVE_LAYER=1 \
CIBW_ARCHS_LINUX=x86_64 \
CIBW_BUILD=cp312-manylinux_x86_64 \
CIBW_ENVIRONMENT="PENGUIN_BURNER_REQUIRE_NATIVE_LAYER=1" \
CIBW_MANYLINUX_X86_64_IMAGE=manylinux_2_28 \
CIBW_BEFORE_ALL_LINUX=$'if command -v dnf >/dev/null 2>&1; then\n  dnf install -y vulkan-headers\nelse\n  yum install -y vulkan-headers\nfi' \
    python3 -m cibuildwheel --platform linux --output-dir "$outdir"
python3 -m twine check "$outdir"/*
