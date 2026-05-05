#!/usr/bin/env bash

set -euo pipefail

workdir="${1:-dist/arch-build}"

rm -rf "$workdir"
mkdir -p "$workdir"
cp packaging/arch/PKGBUILD "$workdir/PKGBUILD"

(
    cd "$workdir"
    makepkg --ignorearch --printsrcinfo > .SRCINFO
    makepkg --ignorearch --verifysource
)
