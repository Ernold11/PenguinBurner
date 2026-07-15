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
outdir="$(cd "$outdir" && pwd)"

tarball="$outdir/$name-$version.tar.gz"

# Archive exactly the committed tree: a working-directory tar can sweep
# untracked local files (vendored checkouts, scratch data) into a published
# SRPM — both a multi-gigabyte tarball and a disclosure hazard.
git archive \
    --format=tar.gz \
    --prefix="$name-$version/" \
    --output="$tarball" \
    HEAD

rpmbuild -bs packaging/rpm/penguin-burner.spec \
    --define "_sourcedir $outdir" \
    --define "_srcrpmdir $outdir"

ls -1 "$outdir"/*.src.rpm
