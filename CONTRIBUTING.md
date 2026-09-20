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

The suite needs the test extra; the GUI tests build real windows, so they also
need a display or `QT_QPA_PLATFORM=offscreen`:

```bash
python -m pip install --user -e '.[test]'
QT_QPA_PLATFORM=offscreen python -m pytest tests/
```

Build the daemon before the Python suite so the socket integration tests run
instead of being skipped:

```bash
scripts/build-daemon.sh
```

Install the same quality tools used by CI, then run the blocking static checks:

```bash
python -m pip install --user -r requirements-quality.txt
scripts/check-feature-static-analysis.sh
```

The routine also requires `ripgrep`, `scc`, `cloc`, and `tokei` on `PATH`.
Ruff 0.16.8, Pyright 1.1.414, and Vulture 2.16 are pinned in
`requirements-quality.txt`; update the pins and Ruff's `required-version` in
`pyproject.toml` together. Repository-wide Pyright errors fail the check.

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

Every pull request runs Python quality/tests, Rust formatting/Clippy/tests and
dependency checks, Arch/CachyOS, supported Fedora and Ubuntu package builds,
Flatpak packaging, host-Python compatibility, and the daemon lifecycle test.
These checks are required on `main`, including for administrators, with the
branch required to be current. There are no path filters or allowed failures
on these gates. Rawhide/devel and published-channel installation checks remain
scheduled drift checks rather than PR gates.

The required status names and protection policy are recorded in
`.github/branch-protection.json`. Keep that file aligned with workflow job
names when changing CI; changing the file alone does not update GitHub's
repository settings.

CachyOS CI refreshes signed databases from one configured mirror at a time.
If a database and signature disagree, it clears that failed CachyOS sync cache
and tries another mirror (up to six). It requires database signatures and
stops before the package build if no mirror verifies. A repository outage is
reported as an infrastructure failure, never converted into a passing check.
Once a database verifies, the chosen mirror stays first and other configured
mirrors remain available for signed package downloads, covering mirrors that
have synced a database before all its referenced packages.

## Hardware safety

PenguinBurner writes real V/F offsets, power limits, and fan control. Test
changes to those paths on hardware you can recover (a bad point can hang the GPU
or force a reboot). See [SECURITY.md](SECURITY.md).
