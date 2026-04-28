# PenguinBurner 0.1.4 Release Notes

## GitHub Release Notes

PenguinBurner 0.1.4 focuses on a safer Auto-UV search path, stronger
performance-mode controls, and clearer install/run workflows.

### Highlights

- MSI Afterburner-like manual curve editor in Linux, with keyboard shortcuts
  and curve-shifting controls in place.
- Performance bias for Automatic Undervolt, especially for older GPUs where
  users may want to maximize FPS.
- "Undocumented" yolo mode for users who want more overclocking adventure.
- Manual silent fan curve editor with single-degree temperature control and
  RPM-level tuning.

### Auto-UV Safety

- Performance/yolo scans no longer move downward in voltage after an unstable
  probe.
- Failed voltage/clock probes are blocked by clock band instead of globally
  banning that voltage forever.
- Persistent unsafe entries cover the failed target clock plus the next two
  lower real V/F clock bins.
- Lower-frequency operation at the same voltage remains available when it has
  not failed.

### Packaging And Local Testing

- Added yolo GUI launchers:
  - `penguin-burner-yolo`
  - `pburn-yolo`
- Documented the clean profile reset command for fresh local testing.
- Verified local wheel/source builds and metadata checks.

## PyPI Release Summary

PenguinBurner 0.1.4 improves Auto-UV safety, adds dedicated yolo GUI launchers,
and documents cleaner local testing workflows.

Highlights:

- MSI Afterburner-like manual V/F curve editor for Linux.
- Automatic Undervolt performance bias for users who want to preserve or
  maximize FPS, especially on older GPUs.
- "Undocumented" yolo mode for more aggressive overclocking experiments.
- Manual silent fan curve editor with single-degree temperature and RPM-level
  tuning.
- Safer performance/yolo Auto-UV recovery after instability.
