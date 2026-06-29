# PenguinBurner 0.5.7

## Changes

- Fixes Flatpak systemd apply/startup units so root systemd runs the installed
  Flatpak Python payload directly instead of running `flatpak run --user`.
- Flatpak startup profiles now install through `penguin-burnerd.service` with an
  autostart payload and remove the legacy `PenguinBurner.service` unit.
