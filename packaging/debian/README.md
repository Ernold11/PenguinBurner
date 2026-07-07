# Ubuntu PPA Packaging

This directory contains the Debian packaging template used by
`scripts/build-deb-source.sh`.

Supported PPA targets:

- Ubuntu 25.10 `questing`
- Ubuntu 26.04 `resolute`

The package is amd64-only and requires NVIDIA driver/userspace packages 580 or
newer.

## Rust root daemon (`penguin-burnerd`)

Since 0.6.x the privileged root daemon is a compiled Rust binary built from the
bundled `burnerd/` crate. `debian/rules` (`override_dh_auto_build`) runs:

```
cargo build --release --locked --manifest-path burnerd/Cargo.toml
```

and installs the result to `/usr/libexec/penguin-burnerd` (0755, root-owned) —
the path `runtime/support/runtime_service.py` discovers first. `cargo` is a
`Build-Depends`. `CARGO_HOME` is redirected into `debian/cargo` so the build
stays inside the tree (matching `Rules-Requires-Root: no`).

### Offline / Launchpad builds need vendored crates — REMAINING STEP

`cargo build --locked` still fetches crate sources from crates.io, and
**Launchpad PPA builders have no network access**, so the source package must
carry the crates. `scripts/build-deb-source.sh` builds the `.orig.tar.gz` from
`git ls-files`, and `burnerd/vendor` / `burnerd/target` are not tracked, so they
are not shipped today. To publish a buildable PPA source package, vendor the
crates into the orig tarball before `dpkg-buildpackage`:

```bash
# 1. Vendor the locked crate set and write cargo's source-replacement config.
cd burnerd
cargo vendor --locked vendor > /tmp/pb-cargo-config.toml
mkdir -p .cargo
mv /tmp/pb-cargo-config.toml .cargo/config.toml
cd ..

# 2. Include burnerd/vendor and burnerd/.cargo when assembling the orig tarball
#    (add them to the tar in scripts/build-deb-source.sh, or unpack the tarball,
#    copy them in, and repack), then run dpkg-buildpackage as usual. With the
#    vendor dir + .cargo/config.toml present, `cargo build --locked` resolves
#    fully offline.
```

Alternatively enable `CARGO_NET_OFFLINE=1` once the vendor dir is in place. The
in-tree `debian/rules` build works out of the box on a networked developer box;
only the network-isolated Launchpad path needs the vendoring step above.
