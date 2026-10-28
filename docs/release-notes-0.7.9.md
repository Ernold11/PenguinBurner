<!-- Cut together with the pyproject.toml / burnerd Cargo.toml version bump. -->

# PenguinBurner 0.7.9

- Add mobile RTD3 deep-sleep handling so the daemon avoids keeping a sleeping
  GPU awake (#40). Special thanks to @Christine1204 for the very long debugging
  session on mobile sleep-state behavior.
- Reapply and verify saved GPU state after suspend and resume (#41).
- Allow Auto-UV to continue safely when power telemetry is unavailable (#48).
- Support Flatpak and pip daemon installation and repair on Bazzite and other
  immutable distributions through the canonical
  `/var/opt/penguin-burner/libexec/penguin-burnerd` path (#51). Thanks to
  @LostFire93 for the debugging session.
- Add per-GPU profiles, filtering, Steam targeting, and serial startup
  application (#46). Active daemon monitoring, adaptive switching, drift
  recovery, and fan control still cover one GPU at a time.
- Attribute RTD3 device-node activity to the target GPU on multi-GPU systems,
  so a process using another card cannot start or keep alive the target's
  deferred runtime profile.
- Lower every GPU-table Performance target proportionally (the RTX 5080's
  2980 → 2950 MHz ratio, about 1%) so the default four-bin rising tail stays
  below each family's clock ceiling; before this it could top out above the
  ceiling on some GPUs.
- Default the Balanced power limit to full board power like Performance, so
  the full scan reuses the balanced descent for the Performance tier and only
  runs its Auto-OC climb. Cap any tier per run from the scan dialog; only
  Efficiency stays capped by default.
- Daemon service install/uninstall/migrate commands ask for authorization
  themselves (pkexec/sudo) when not run as root, fixing the
  ModuleNotFoundError a bare sudo hit on pip installs.
