#!/usr/bin/env bash

set -euo pipefail

outdir="${1:-dist/rpm}"
name="penguin-burner"
version="$(
    python3 - <<'PY'
import tomllib
from pathlib import Path

metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
print(metadata["project"]["version"])
PY
)"

rm -rf "$outdir"
mkdir -p "$outdir"

tarball="$outdir/$name-$version.tar.gz"

tar \
    --exclude=.git \
    --exclude=.copr \
    --exclude=dist \
    --exclude=build \
    --exclude='*.egg-info' \
    --transform "s,^.,$name-$version," \
    -czf "$tarball" .

rpmbuild -bs packaging/rpm/penguin-burner.spec \
    --define "_sourcedir $PWD/$outdir" \
    --define "_srcrpmdir $PWD/$outdir"

ls -1 "$outdir"/*.src.rpm
