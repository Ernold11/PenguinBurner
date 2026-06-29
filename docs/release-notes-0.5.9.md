# PenguinBurner 0.5.9

## Changes

- Fixed multiple regressions for Flatpak.
- Restored a single `penguin-burnerd.service` daemon path for Flatpak apply,
  Auto-UV, migration, and runtime profile actions.
- Fixed the Flatpak Steam wrapper so short launch options such as
  `PB_OVERLAY=1 PENGUIN_BURNER %command%` work with overlay and latency paths.
