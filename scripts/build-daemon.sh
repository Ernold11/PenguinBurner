#!/usr/bin/env bash

# Build the PenguinBurner root daemon (Rust crate in burnerd/) for local/dev
# use. The elevated service setup copies this release build atomically to the
# root-owned /var/opt/penguin-burner/libexec/penguin-burnerd path; the unit
# never executes the checkout copy directly.

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# --locked pins the committed burnerd/Cargo.lock so a dev build matches what the
# native packages compile.
cargo build --release --locked --manifest-path "$root/burnerd/Cargo.toml"

echo "built: $root/burnerd/target/release/penguin-burnerd"
