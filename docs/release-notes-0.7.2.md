<!-- Prepared draft for the upcoming 0.7.2 release — not yet published.
     Cut together with the pyproject.toml / burnerd Cargo.toml version bump. -->

# PenguinBurner 0.7.2

## Highlights

- **One-scan, all tiers.** The adaptive **All tiers** scan discovers and
  verifies the Efficiency, Balanced, and Performance profiles in a single pass,
  producing a complete set ready for adaptive switching without three separate
  runs.
- **Per-game Steam settings.** Each game gets its own Auto-UV mode and its own
  **adaptive pre-frame-generation FPS target**, applied automatically at launch
  through the wrapper. A one-click **All games** menu enables or disables the
  wrapper across the whole library, and a single Steam-style Play/Stop button
  reflects the live session. Only one game may be active at a time.
- **Adaptive Auto-UV works with a single saved tier**, instead of requiring at
  least two.
- **Laptop / mobile GPU support.** On GPUs with a fixed board power limit the
  power-limit control is grayed out and scans run at the stock limit, and
  restore-to-stock never tries to set an unsettable limit.

## Fixes

- **Boot profile preserved across the flatpak daemon migration.** Upgrading from
  0.6.x no longer drops the apply-on-startup profile.
- **In-game overlay layer survives flatpak updates.** The Vulkan layer manifest
  is now pinned to the flatpak `active` symlink instead of a commit-specific
  deploy path, which previously dangled after an update and could hang wrapped
  Vulkan games until the wrappers were reinstalled.
- The Performance rising tail defaults to 4 bins (was 6): with the per-tier scan
  reaching the full clock lock, 6 bins overshot the intended ceiling in
  gameplay.

## Other

- Final verification now soaks longer for the more aggressive tiers by default:
  efficiency 1 min, balanced 3 min, performance 5 min. `--auto-uv-final-verification-s`
  still overrides every tier with a single duration.
- The Rust daemon crate version now tracks the release (checked at release
  time), so the daemon reports the same version as the app.

Auto-UV tuning behavior — candidate selection, verification, and saved profile
shape — is otherwise unchanged from 0.7.1.
