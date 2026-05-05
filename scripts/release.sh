#!/usr/bin/env bash

set -euo pipefail

if [ "$#" -ne 1 ]; then
    echo "usage: $0 VERSION" >&2
    exit 2
fi

version="$1"
tag="v$version"

require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "missing required command: $1" >&2
        exit 1
    fi
}

require_command gh
require_command rpmbuild
require_command copr-cli
require_command python3

scripts/check-release-version.sh "$version" >/dev/null

if [ -n "$(git status --porcelain)" ]; then
    echo "working tree is not clean; commit or stash changes before release" >&2
    git status --short >&2
    exit 1
fi

if gh release view "$tag" >/dev/null 2>&1; then
    echo "GitHub release already exists: $tag" >&2
    exit 1
fi

scripts/build-python-dist.sh dist/python
scripts/build-srpm.sh dist/rpm

gh release create "$tag" \
    dist/python/* \
    dist/rpm/* \
    --target "$(git rev-parse HEAD)" \
    --title "PenguinBurner $version" \
    --notes-file "docs/release-notes-$version.md"

scripts/publish-copr.sh "$(find dist/rpm -maxdepth 1 -name '*.src.rpm' -print -quit)"

echo "Released $version."
echo "GitHub release: $(gh release view "$tag" --json url --jq .url)"
echo "COPR project: https://copr.fedorainfracloud.org/coprs/jpietek/penguin-burner/"
