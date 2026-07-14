<!-- Cut together with the pyproject.toml / burnerd Cargo.toml version bump. -->

# PenguinBurner 0.7.3

- Flatpak: Steam host integration (wrapper, Vulkan layer manifest, NVAPI shim)
  is installed only when a host Steam installation is detected — Steam-less
  hosts stay untouched.
- A failed Steam integration repair no longer blocks GUI startup or privileged
  actions; Auto-UV, profiles, and fan control work without Steam.
- Docs: native installs (pip / COPR / AUR / PPA) are now the recommended
  medium; the Flatpak is documented as a fallback for immutable distros.
