#!/usr/bin/env bash

# Build the PenguinBurner root daemon (Rust crate in burnerd/) for local/dev
# use. When no packaged /usr/libexec/penguin-burnerd is installed,
# runtime/support/runtime_service.py falls back to the cargo release build this
# script produces at burnerd/target/release/penguin-burnerd.

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# --locked pins the committed burnerd/Cargo.lock so a dev build matches what the
# native packages compile.
cargo build --release --locked --manifest-path "$root/burnerd/Cargo.toml"

echo "built: $root/burnerd/target/release/penguin-burnerd"
