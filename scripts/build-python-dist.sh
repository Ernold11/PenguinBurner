#!/usr/bin/env bash

set -euo pipefail

outdir="${1:-dist/python}"

rm -rf "$outdir"
mkdir -p "$outdir"

python3 -m pip install --upgrade build cibuildwheel twine

# cibuildwheel defaults to docker; use podman when that is what the host has.
if [ -z "${CIBW_CONTAINER_ENGINE:-}" ] && ! command -v docker >/dev/null 2>&1 \
        && command -v podman >/dev/null 2>&1; then
    export CIBW_CONTAINER_ENGINE=podman
fi
PENGUIN_BURNER_REQUIRE_NATIVE_LAYER=1 \
    python3 -m build --sdist --outdir "$outdir"
# MinGW cross-compiles the NVAPI latency shim (overlay/nvapi_shim/nvapi64.dll)
# into the wheel; REQUIRE_NVAPI_SHIM makes a missing toolchain fail the build
# instead of shipping the in-game latency feature hollow (release-plan B2).
# manylinux_2_28 is AlmaLinux 8: mingw lives in EPEL, and its old MinGW needs
# static winpthreads for the -static link.
# The Rust root daemon (burnerd/ -> runtime/daemon_bin/penguin-burnerd) is built
# by REQUIRE_DAEMON: AlmaLinux 8's cargo is too old for edition 2021, so install
# a pinned stable via rustup into /opt/rust and put it on PATH for the build.
PENGUIN_BURNER_REQUIRE_NATIVE_LAYER=1 \
CIBW_ARCHS_LINUX=x86_64 \
CIBW_BUILD=cp312-manylinux_x86_64 \
CIBW_ENVIRONMENT="PENGUIN_BURNER_REQUIRE_NATIVE_LAYER=1 PENGUIN_BURNER_REQUIRE_NVAPI_SHIM=1 PENGUIN_BURNER_REQUIRE_DAEMON=1 CARGO_HOME=/opt/rust/cargo RUSTUP_HOME=/opt/rust/rustup PATH=/opt/rust/cargo/bin:\$PATH" \
CIBW_MANYLINUX_X86_64_IMAGE=manylinux_2_28 \
CIBW_BEFORE_ALL_LINUX=$'if command -v dnf >/dev/null 2>&1; then\n  dnf install -y epel-release\n  dnf install -y vulkan-headers mingw64-gcc-c++ mingw64-winpthreads-static\nelse\n  yum install -y epel-release\n  yum install -y vulkan-headers mingw64-gcc-c++ mingw64-winpthreads-static\nfi\nexport RUSTUP_HOME=/opt/rust/rustup CARGO_HOME=/opt/rust/cargo\ncurl --proto \'=https\' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain 1.82.0 --no-modify-path' \
    python3 -m cibuildwheel --platform linux --output-dir "$outdir"
python3 -m twine check "$outdir"/*
