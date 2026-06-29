# PenguinBurner 0.5.6

## Changes

- Fixes Flatpak profile apply/startup actions by letting host systemd start the
  Flatpak runtime instead of launching a nested Flatpak process under `pkexec`.
- Fixes Flatpak hardware-service setup to install `penguin-burnerd.service`
  through host systemd, avoiding the same sandbox file-descriptor failure.
