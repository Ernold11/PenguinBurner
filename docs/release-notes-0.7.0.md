<!-- Prepared draft for the upcoming 0.7.0 release — not yet published.
     Cut together with the pyproject.toml version bump. -->

# PenguinBurner 0.7.0

## Highlights

- The root hardware daemon is now a compiled Rust binary (`penguin-burnerd`),
  replacing the Python daemon and profile engine. It is a single static,
  memory-safe process supervised by a systemd watchdog: if it ever hangs, it is
  restarted automatically and re-applies your last profile.
- Every privileged GPU operation now goes through the daemon's local socket —
  applying V/F curves, power limits, memory offsets, fan control, locked
  clocks, restore to stock, profile verification, and profile deletion. The
  `pkexec` password prompts for these actions are gone; only the one-time
  service install still asks for elevation.
- Auto-UV scans and profile verification no longer run as root. The daemon
  drops the scan and verification processes to your desktop user; only the
  GPU writes themselves are performed by the root daemon.
- The daemon ships with every install method: bundled in the PyPI wheel
  (installed to `/usr/libexec` during the one-time service setup), built into
  the Flatpak sandbox, and built from source by the COPR/AUR/PPA packages.
- Fixed a driver interaction where applying a memory offset silently wiped the
  core per-point V/F curve, so an applied undervolt could run the stock curve
  under load. Memory offsets are now applied before the V/F curve and the
  curve is re-applied after any genuine memory-offset rewrite — UV curves now
  hold under load.

Auto-UV tuning behavior is unchanged: the Efficiency, Balanced, and
Performance sweeps, candidate selection, verification, and saved profiles are
identical to 0.6.x. The daemon socket API is also unchanged, so existing
GUI/CLI workflows keep working as before.
