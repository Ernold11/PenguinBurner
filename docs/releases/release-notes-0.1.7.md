# PenguinBurner 0.1.7 Release Notes

## GitHub Release Notes

PenguinBurner 0.1.7 focuses on Auto-UV stability edge cases reported from real
systems: time-based final verification, NVIDIA PRIME laptop routing, and safer
hard-hang handling during lower-voltage probing.

### Highlights

- Final verification now honors the configured time budget instead of deriving
  Q2RTX timedemo loop counts from system-specific FPS.
- Q2RTX probes request NVIDIA PRIME render offload so hybrid laptop systems run
  the workload on the selected NVIDIA GPU.
- Auto-UV now detects when Q2RTX is not loading the selected NVIDIA GPU and
  stops without blacklisting the tested voltage.
- Stale in-progress markers from obvious normal candidate hard hangs can now be
  converted into unsafe voltage/clock cache entries on the next run.
- Previous hard-crash points are cached conservatively so later scans avoid the
  unsafe voltage/clock band.
- RTX 3080 now gets a conservative 10% Auto-UV voltage-drop default, equivalent
  to a 900 mV floor from the 1000 mV reference.

### Auto-UV Behavior

- Short and final Q2RTX probes use duration-based execution, leaving loop
  counting to the live workload rather than precomputing loops from prior FPS.
- The crash cache remains conservative: normal candidate hard-hang markers are
  trusted only when they include enough voltage-drop and near-baseline-clock
  context.
- Selected-GPU idle detection is treated as a workload routing problem, not as
  evidence that the tested voltage is unsafe.
- Base-load diagnostics now call out likely selected-GPU light-load cases so
  users can distinguish GPU routing and power-profile issues from undervolt
  instability.

### Packaging And Local Testing

- Package metadata is prepared for version 0.1.7.
- Local wheel and source distributions should pass `twine check`.
- AUR should be updated after the GitHub `v0.1.7` tag exists so the source
  tarball and `.SRCINFO` point at the correct version.

## PyPI Release Summary

PenguinBurner 0.1.7 fixes time-based final verification, improves NVIDIA PRIME
laptop workload routing, records obvious interrupted-probe hard hangs as unsafe
voltage/clock bands and uses a
more conservative RTX 3080 Auto-UV voltage-drop default.
