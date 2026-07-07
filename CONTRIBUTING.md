# Contributing to PenguinBurner

Thanks for your interest. PenguinBurner is an NVIDIA-on-Linux GPU tuning tool;
contributions that improve tuning, the overlay, packaging, or docs are welcome.

## Development setup

```bash
git clone https://github.com/jpietek/PenguinBurner
cd PenguinBurner
python -m pip install --user -e .
```

The privileged root daemon is a Rust crate in `burnerd/`. Building it (and
building the wheel, which bundles the compiled binary) needs a Rust toolchain
(`cargo`):

```bash
scripts/build-daemon.sh   # cargo build --release --locked in burnerd/
```

## Tests

```bash
python -m pytest tests/
```

For changes under `burnerd/`, also run the Rust gates:

```bash
cd burnerd
cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo test && cargo deny check
```

Run the docs flag check (catches CLI flags documented but not in the parser):

```bash
python -m pytest tests/test_docs_cli_flags.py
```

## Style

- Match the surrounding code; keep functions small and readable.
- Lint with `ruff` before opening a PR.
- Keep user docs in `docs/features/` concise; internal notes stay outside the tracked repo.

## Pull requests

- Branch from `main`, keep PRs focused, and describe what you changed and why.
- If you touch tuning or hardware paths, say how you tested on real hardware.

## Hardware safety

PenguinBurner writes real V/F offsets, power limits, and fan control. Test
changes to those paths on hardware you can recover (a bad point can hang the GPU
or force a reboot). See [SECURITY.md](SECURITY.md).
