#!/usr/bin/env bash

set -euo pipefail

if [ "$#" -ne 1 ]; then
    echo "usage: $0 VERSION" >&2
    exit 2
fi

requested_version="$1"
package_version="$(
    python3 - <<'PY'
import tomllib
from pathlib import Path

metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
print(metadata["project"]["version"])
PY
)"

if [ "$requested_version" != "$package_version" ]; then
    echo "release version does not match pyproject.toml: requested=$requested_version pyproject=$package_version" >&2
    exit 1
fi

# The Rust daemon reports its own crate version over the socket, so it must
# track the release too (a drift here shipped a 0.7.2 build whose daemon still
# reported 0.7.1).
burnerd_version="$(
    sed -n 's/^version = "\(.*\)"/\1/p' burnerd/Cargo.toml | head -1
)"
if [ "$requested_version" != "$burnerd_version" ]; then
    echo "release version does not match burnerd/Cargo.toml: requested=$requested_version burnerd=$burnerd_version" >&2
    exit 1
fi

if [ ! -f "docs/release-notes-$requested_version.md" ]; then
    echo "missing docs/release-notes-$requested_version.md" >&2
    exit 1
fi

echo "$package_version"
