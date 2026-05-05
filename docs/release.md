# Release

PenguinBurner GitHub/COPR releases are driven by a local shell script. The
script builds Python distributions, builds a Fedora source RPM, creates the
GitHub release, and submits the source RPM to COPR.

PyPI publishing stays in the existing `Publish Python package` workflow, which
is triggered by the published GitHub release.

## Required Local Tools

- `gh`, authenticated with permission to create releases.
- `copr-cli`, authenticated with `~/.config/copr`.
- `rpmbuild`.
- Keep the existing PyPI workflow credentials/configuration unchanged.

## One-Command Release

From a clean checkout on the release commit:

```bash
scripts/release.sh 0.1.5
```

The version must match `pyproject.toml`, and
`docs/release-notes-<version>.md` must exist.

## Fedora Package

The RPM is x86_64-only and has a hard runtime dependency on RPM Fusion NVIDIA
driver packages:

```text
xorg-x11-drv-nvidia-cuda >= 3:580
or
xorg-x11-drv-nvidia-580xx-cuda >= 3:580
```

Users must enable RPM Fusion nonfree and the COPR repo before installing.
