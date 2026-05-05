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

if [ ! -f "docs/release-notes-$requested_version.md" ]; then
    echo "missing docs/release-notes-$requested_version.md" >&2
    exit 1
fi

echo "$package_version"
