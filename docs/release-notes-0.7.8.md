<!-- Cut together with the pyproject.toml / burnerd Cargo.toml version bump. -->

# PenguinBurner 0.7.8

- Hotfixes mobile GPU power-limit detection so unsupported controls stay
  disabled and fresh profiles apply without rejected power-limit writes.
- Saves and replays startup profiles per GPU UUID, with serial multi-GPU boot
  recovery, explicit Profiles-tab targeting, and per-card replay diagnostics.
