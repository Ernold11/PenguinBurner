#!/usr/bin/env python3
"""Regenerate cargo-sources.json for the Flatpak offline daemon build.

Flathub builds run with no network, so the Rust daemon (burnerd/) must be
compiled against pre-declared crate sources. This emits the "flatpak-cargo"
vendored-sources layout that the manifest's penguin-burnerd module extracts
into `cargo/vendor` (+ a `cargo/config` that redirects crates.io to it), so
`cargo build --offline --locked` resolves every dependency locally.

The output is byte-for-byte identical to the canonical upstream generator
(flatpak-builder-tools/cargo/flatpak-cargo-generator.py) for this repo's
lockfile, which contains only crates.io registry dependencies. Unlike the
upstream tool this script needs no third-party packages and no network: every
crate's sha256 already lives in Cargo.lock, so nothing is downloaded.

Refresh whenever burnerd/Cargo.lock changes:

    python3 packaging/flatpak/gen-cargo-sources.py

(run from the repo root; writes packaging/flatpak/cargo-sources.json). If a
git dependency is ever added to the crate, this script will stop with an error
-- regenerate with the upstream flatpak-cargo-generator.py instead, which can
resolve git sources.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path

CRATES_IO = "https://static.crates.io/crates"
CARGO_HOME = "cargo"
CARGO_CRATES = f"{CARGO_HOME}/vendor"
VENDORED_SOURCES = "vendored-sources"

# tomlkit.dumps({"source": {"vendored-sources": {"directory": "cargo/vendor"},
#                           "crates-io": {"replace-with": "vendored-sources"}}})
# reproduced verbatim so this script needs no tomlkit dependency.
CARGO_CONFIG = (
    "[source.vendored-sources]\n"
    f'directory = "{CARGO_CRATES}"\n'
    "\n"
    "[source.crates-io]\n"
    f'replace-with = "{VENDORED_SOURCES}"\n'
)


def generate_sources(cargo_lock: dict) -> list[dict]:
    sources: list[dict] = []
    for package in cargo_lock["package"]:
        name = package["name"]
        version = package["version"]
        source = package.get("source")
        if source is None:
            # Path/workspace members (the root crate) carry no source.
            continue
        if source.startswith("git+"):
            raise SystemExit(
                f"{name} {version} is a git dependency; this stdlib-only "
                "generator only handles crates.io. Regenerate with the "
                "upstream flatpak-cargo-generator.py instead."
            )
        checksum = package.get("checksum")
        if checksum is None:
            raise SystemExit(f"{name} {version} has a source but no checksum")
        sources.append(
            {
                "type": "archive",
                "archive-type": "tar-gzip",
                "url": f"{CRATES_IO}/{name}/{name}-{version}.crate",
                "sha256": checksum,
                "dest": f"{CARGO_CRATES}/{name}-{version}",
            }
        )
        sources.append(
            {
                "type": "inline",
                "contents": json.dumps({"package": checksum, "files": {}}),
                "dest": f"{CARGO_CRATES}/{name}-{version}",
                "dest-filename": ".cargo-checksum.json",
            }
        )
    sources.append(
        {
            "type": "inline",
            "contents": CARGO_CONFIG,
            "dest": CARGO_HOME,
            "dest-filename": "config",
        }
    )
    return sources


def main() -> None:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "cargo_lock",
        nargs="?",
        default=str(here.parent.parent / "burnerd" / "Cargo.lock"),
        help="Path to Cargo.lock (default: ../../burnerd/Cargo.lock)",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=str(here / "cargo-sources.json"),
        help="Where to write the generated sources (default: ./cargo-sources.json)",
    )
    args = parser.parse_args()

    with open(args.cargo_lock, "rb") as f:
        cargo_lock = tomllib.load(f)

    sources = generate_sources(cargo_lock)
    with open(args.output, "w", encoding="utf-8") as out:
        json.dump(sources, out, indent=4, sort_keys=False)

    crate_count = sum(1 for s in sources if s["type"] == "archive")
    print(f"Wrote {args.output}: {crate_count} crates", file=sys.stderr)


if __name__ == "__main__":
    main()
