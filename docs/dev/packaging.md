# Packaging Strategy

PyPI is the primary distribution path for now. Native packages are useful for
users who want distro-managed dependencies and system integration, but they add
a real maintenance matrix across Debian/Ubuntu, Fedora/RPM, and Arch/CachyOS.

## Current Recommendation

- Publish and test the Python package first.
- Install the package normally; the GUI dependencies are part of the base
  package:

```bash
python -m pip install penguin-burner
```

- Keep the `ui` extra as a compatibility alias for older install commands, but
  do not require users to remember it.
- Prefer `pipx` for end-user installs when available, because it keeps Python
  dependencies isolated while still exposing console commands.
- Keep native packages as a later distribution layer, not as the source of truth.

## Native Packages Worth Considering Later

- Ubuntu 26.04 `.deb`
- Fedora `.rpm`
- Arch/CachyOS `PKGBUILD`

Do not add these until there is enough user demand to justify maintaining and
testing them. Each package should wrap the same Python project metadata rather
than duplicating dependency/version logic.

## Packaging Readiness Requirements

- CLI entry point works after a normal wheel install.
- GUI entry points work after a normal wheel install.
- `penguin_burner.sh` and `PenguinBurner.service` are installed as package data
  under `share/penguin-burner/`.
- Systemd install resolves the installed launcher script from package data.
- Q2RTX/CUDA workload downloads remain runtime-managed by PenguinBurner, not
  bundled into distro packages.

## Native Package Maintenance Rule

Only maintain native packaging if CI can build the package from the same source
tree and run at least the Python test suite. Avoid publishing distro packages
manually from a developer machine.
