# PenguinBurner 0.2.5 Release Notes

## GitHub Release Notes

PenguinBurner 0.2.5 fixes LACT export of saved Auto-UV profiles.

### LACT Export

- LACT exports now respect LACT's Nvidia V/F offset behavior by clamping
  exported clocks to `base_mhz + 1000` MHz by default and reporting a warning
  when a generated point is reduced.
- `--export-lact-config` now honors `--auto-uv-profile`, so the CLI can export a
  selected saved Auto-UV profile by profile id, candidate id, JSON path,
  `active`, or `latest`.
- `--list-auto-uv-profiles` is now visible in CLI help so users can list saved
  profiles before exporting one to LACT.
- `--lact-max-vf-offset-mhz` allows overriding the default LACT Nvidia V/F
  offset ceiling for systems where the driver reports a different limit.

## PyPI Release Summary

PenguinBurner 0.2.5 fixes LACT export for selected Auto-UV profiles and clamps
exported Nvidia V/F clocks to LACT's default offset limit before writing
`gpu_vf_curve`.
