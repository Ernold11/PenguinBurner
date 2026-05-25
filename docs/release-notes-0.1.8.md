# PenguinBurner 0.1.8 Release Notes

## GitHub Release Notes

PenguinBurner 0.1.8 is a cleanup and Auto-UV behavior release. It removes the
old Auto-UV3 package surface, keeps Balanced and Efficiency focused on the
undervolt pass, and adds the Performance Auto-OC pass as separate backend logic.

### Highlights

- Auto-UV internals now live under `auto_uv`; the old `auto_uv3` tracked files
  are removed from the package.
- Performance mode now runs the balanced undervolt flow first, then performs a
  bounded Auto-OC ladder up to the editable voltage and clock targets.
- Auto-OC search optimizes measured Q2RTX effective clock while the final
  Performance result table is sorted by FPS.
- The Auto-UV run table shows stable second-based status progress, keeps
  Decision and Status in the correct columns, and avoids duplicate probe rows.
- Performance runs now show Auto-OC clock progress in the table as applied MHz
  over the allowed MHz target headroom, while non-Auto-OC rows show `0/0`.
- The Performance setup UI keeps only the two editable Auto-OC target fields:
  voltage target and clock target.

### Packaging And Local Testing

- Package metadata is prepared for version 0.1.8.
- The new `auto_oc` package is included in Python and RPM package metadata.
- Local tests pass with `369 passed`.
- Local wheel and source distributions should pass `twine check`.

## PyPI Release Summary

PenguinBurner 0.1.8 cleans up Auto-UV internals, removes stale Auto-UV3 tracked
files, adds a separate Performance Auto-OC pass after balanced undervolting, and
fixes Auto-UV table progress/status/OC reporting.
