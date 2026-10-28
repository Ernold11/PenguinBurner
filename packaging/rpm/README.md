# Fedora / COPR Packaging

This directory contains `penguin-burner.spec`, the RPM used by the
[COPR project](https://copr.fedorainfracloud.org/coprs/jpietek/penguin-burner/)
and by `scripts/build-srpm.sh`.

## Rust root daemon build dependency

Since 0.6.x the privileged root daemon (`penguin-burnerd`) is a compiled Rust
binary built from the bundled `burnerd/` crate. `%build` runs:

```
cargo build --release --locked --manifest-path burnerd/Cargo.toml
```

and `%install` places the binary at `%{_libexecdir}/penguin-burnerd`
(`/usr/libexec/penguin-burnerd`, 0755, root-owned). This package-owned file is
an install source: explicit hardware-service setup copies it to
`/var/opt/penguin-burner/libexec/penguin-burnerd`, which is the only path
generated systemd units execute.

`--locked` pins the committed `burnerd/Cargo.lock`, but cargo still fetches the
crate sources from crates.io at build time. **The COPR project must have
"Enable internet access during builds" turned on** (Settings → Build options),
otherwise the sandboxed mock chroot has no network and the `cargo build` step
fails resolving dependencies. A local `rpmbuild`/`mock` with network works
out of the box.

### Fully offline builds (optional)

To build without network, vendor the crates ahead of time and point cargo at
them:

```bash
cd burnerd
cargo vendor vendor            # writes vendor/ and prints a [source] block
mkdir -p .cargo
cargo vendor vendor >> .cargo/config.toml
```

then add `%cargo_prep`-style source replacement (or ship the vendor tree as an
extra `SourceN:` tarball). This is only needed for network-isolated builders;
COPR with internet access enabled does not require it.
