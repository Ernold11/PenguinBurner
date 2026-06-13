# PenguinBurner 0.2 Release Notes

## GitHub Release Notes

PenguinBurner 0.2 is a major Auto-UV profile release. It revises the
Efficiency and Performance profile behavior, updates release packaging for
version 0.2, and includes cbro33's memory OC offset consistency fix.

### Highlights

- Efficiency profile behavior was revised to tune for the lowest stable voltage
  while retaining as much loaded core clock as possible.
- Performance profiles were revised around clearer voltage and clock targets,
  with the Auto-OC pass separated from the lower-voltage sweep.
- Memory OC offset is now applied consistently after runtime V/F curve reapplies,
  thanks to cbro33's pull request.
- Auto-UV packaging includes the revised efficiency tuning package and cleaned
  Auto-UV module layout.
- Runtime fan-loop coverage now includes memory offset reapply behavior.

### Packaging And Local Testing

- Package metadata is prepared for version 0.2.
- GitHub release tag: `v0.2`.
- Fedora COPR package version: `0.2-2`.
- Local wheel and source distributions should pass `twine check`.

## PyPI Release Summary

PenguinBurner 0.2 revises the Efficiency profile to find the lowest stable
voltage while retaining as much clock as possible, updates Performance profiles
around clearer voltage and clock targets, and consistently reapplies memory OC
offsets after runtime V/F curve reapplies thanks to cbro33's pull request.

## Fedora COPR Release Summary

PenguinBurner 0.2 is a major Auto-UV profile update for Fedora COPR. Efficiency
now targets the lowest stable voltage while preserving as much loaded clock as
possible, Performance profiles use revised voltage and clock targets, and
memory OC offsets are consistently reapplied with runtime V/F curve recovery.
