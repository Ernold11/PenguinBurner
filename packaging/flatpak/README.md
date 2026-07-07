# Flatpak Packaging

This directory contains `io.github.jpietek.PenguinBurner.yml`, the Flatpak
manifest (targeting `org.freedesktop.Platform//25.08`) plus its desktop entry,
AppStream metainfo, and the offline crate sources for the root daemon.

## Root daemon (`penguin-burnerd`) — offline Rust build

Since 0.6.x the privileged root daemon is a compiled Rust binary built from the
bundled `burnerd/` crate. The manifest builds it inside the sandbox with the
`org.freedesktop.Sdk.Extension.rust-stable` SDK extension and installs it to
`/app/libexec/penguin-burnerd` (0755) — the module named `penguin-burnerd`.

Flathub builds run with **no network access**, so the build cannot fetch crates
from crates.io. Instead the crate sources are pre-declared, in the established
[flatpak-cargo][flatpak-builder-tools] "vendored-sources" layout:

- **`cargo-sources.json`** is a generated list of Flatpak `sources`. Each
  crate becomes an `archive` entry (`https://static.crates.io/crates/…`, pinned
  by the sha256 from `burnerd/Cargo.lock`) that extracts to `cargo/vendor/<crate>`
  with its `.cargo-checksum.json`. A final `inline` source writes a
  `cargo/config` that redirects `crates.io` to `cargo/vendor`.
- The `penguin-burnerd` module lists `cargo-sources.json` as a source and sets
  `CARGO_HOME=/run/build/penguin-burnerd/cargo`, so `cargo build --offline
  --locked` resolves every dependency from the extracted vendor tree.
- `--locked` pins the committed `burnerd/Cargo.lock`; the archive sha256s are
  the lockfile's own checksums, so the two can never silently drift.

This mirrors how Flathub Rust apps ship (declared remote crate archives, not a
checked-in `vendor/` tree), keeping the source package small and the build
reproducible/offline.

### Refreshing `cargo-sources.json`

Regenerate whenever `burnerd/Cargo.lock` changes (new/updated/removed crate):

```bash
python3 packaging/flatpak/gen-cargo-sources.py
```

Run from the repo root; it reads `burnerd/Cargo.lock` and rewrites
`packaging/flatpak/cargo-sources.json`. The script is standard-library only
(no `aiohttp`/`tomlkit`, no network) because every crate's sha256 already lives
in the lockfile — nothing is downloaded. For this repo's crates.io-only
lockfile its output is **byte-for-byte identical** to the canonical upstream
generator, so a Flathub reviewer regenerating with the upstream tool sees no
diff:

```bash
# Equivalent canonical command (needs aiohttp/PyYAML/tomlkit + is used by
# Flathub CI). Both produce the same cargo-sources.json here.
uv run https://raw.githubusercontent.com/flatpak/flatpak-builder-tools/master/cargo/flatpak-cargo-generator.py \
    burnerd/Cargo.lock -o packaging/flatpak/cargo-sources.json
```

If a **git** dependency is ever added to `burnerd/Cargo.toml`, the
standard-library script stops with an error — use the upstream
`flatpak-cargo-generator.py` instead, which can resolve git sources (and commit
its output here).

[flatpak-builder-tools]: https://github.com/flatpak/flatpak-builder-tools/tree/master/cargo
