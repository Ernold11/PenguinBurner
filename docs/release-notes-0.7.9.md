<!-- Cut together with the pyproject.toml / burnerd Cargo.toml version bump. -->

# PenguinBurner 0.7.9

- Add mobile RTD3 deep-sleep handling so the daemon avoids keeping a sleeping
  GPU awake (#40). Special thanks to @Christine1204 for the very long debugging
  session on mobile sleep-state behavior.
- Reapply and verify saved GPU state after suspend and resume (#41).
- Allow Auto-UV to continue safely when power telemetry is unavailable (#48).
- Partly fix multi-GPU support with per-GPU profiles, filtering, Steam
  targeting, and serial startup application. The Rust daemon still has one
  active monitoring, adaptive-switching, drift-recovery, and fan-control
  engine, so those active features cover only one GPU at a time (#49).
