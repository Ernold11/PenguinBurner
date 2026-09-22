# PenguinBurner 0.8.3

- **32-bit games get the overlay from every install path.** The Flatpak now ships the 32-bit Vulkan layer too, so Gorogoa and older GOG titles show the HUD, latency and Adaptive markers whether PenguinBurner came from PyPI, COPR, the AUR, the PPA or Flatpak.

0.8.2 shipped the layer only through pip and the distro packages. Each build
environment needed its own 32-bit toolchain, and the Flatpak SDK has none at
all, so its layer is built in the same manylinux image the wheel uses. Every
packaging recipe now fails rather than shipping an overlay that cannot reach a
32-bit game.

**Faugus Launcher is coming next.** Which launcher should we support after that?
Tell us what you use in
[Discussions](https://github.com/jpietek/PenguinBurner/discussions).
