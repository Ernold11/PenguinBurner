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
