# PenguinBurner 0.2.5 Release Notes

## GitHub Release Notes

PenguinBurner 0.2.5 fixes LACT export of saved Auto-UV profiles.

### LACT Export

- LACT exports now respect LACT's Nvidia V/F offset behavior by clamping
  exported clocks to `base_mhz + 1000` MHz by default and reporting a warning
  when a generated point is reduced.
- The LACT export command now honors Auto-UV profile selection, so users can
  export a selected saved profile by profile id, candidate id, JSON path,
  active profile, or latest profile.
- `--list-auto-uv-profiles` is now visible in CLI help so users can list saved
  profiles before exporting one to LACT.
- LACT export allows overriding the default Nvidia V/F offset ceiling for
  systems where the driver reports a different limit.

## PyPI Release Summary

PenguinBurner 0.2.5 fixes LACT export for selected Auto-UV profiles and clamps
exported Nvidia V/F clocks to LACT's default offset limit before writing
`gpu_vf_curve`.
