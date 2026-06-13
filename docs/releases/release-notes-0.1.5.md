# PenguinBurner 0.1.5 Release Notes

## GitHub Release Notes

PenguinBurner 0.1.5 focuses on the refactored Qt workflow, safer NVIDIA
voltage handling, and clearer Auto-UV final selection.

### Highlights

- Qt UI workflow for Auto-UV, profile verification, profile apply, LACT export,
  and manual curve editing.
- Hidden NVML voltage probing is disabled because static driver offsets are too
  fragile; the hidden NVAPI voltage path is used instead when available.
- Auto-UV short verification defaults to 20 seconds for faster local checks.
- Final candidate selection sorts performance and efficiency modes by their
  intended metrics, with manual column sorting available in the modal.
- Manual V/F curve Ctrl+Click adds a snapped editable control point on the real
  V/F bin grid.
- The profile context menu keeps V/F curve editing as the main curve entry
  point, without a separate read-only curve view action.

### Auto-UV Behavior

- Performance mode can probe higher voltage/clock candidates at the voltage floor.
- Performance probing can test higher voltage/clock candidates and stops when
  FPS stops improving.
- Efficiency mode keeps the lower voltage-focused policy.
- Final and long verification workload ratios remain anchored to the previous
  long-check reference, independent of the shorter default probe duration.

### Packaging And Local Testing

- Package metadata is prepared for version 0.1.5.
- Local wheel and source distributions are intended to be checked with
  `twine check` before any PyPI upload.
- This release preparation does not require pushing the tag or publishing to
  PyPI until the local package has been tested.

## PyPI Release Summary

PenguinBurner 0.1.5 adds the refactored Qt workflow, safer voltage probing, a
20-second default short verification, corrected final-candidate sorting, and
snapped manual V/F control-point editing.
