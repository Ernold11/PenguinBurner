# PenguinBurner 0.2.1 Release Notes

## GitHub Release Notes

PenguinBurner 0.2.1 improves Auto-UV tuning for multi-GPU systems and explicit
low-voltage curve experiments.

### Auto-UV And GPU Selection

- Added explicit GPU selection across the CLI/GUI lifecycle. `--gpu-index N`
  binds control, telemetry, Q2RTX, CUDA, verification, and runtime actions to
  one NVIDIA GPU. The GUI also accepts `--gpu-index N` and `--index N`, falling
  back to `[gpu].index` in the runtime config when no launch option is present.
- Q2RTX now receives dynamically derived NVIDIA device selectors for the chosen
  GPU instead of relying on display defaults or hardcoded GPU ids.
- Added GUI/CLI support for `--auto-uv-tail-rise-bins N`.
- Explicit flat-tail floor searches now behave as requested: when
  `--auto-uv-tail-rise-bins 0` and `--auto-uv-min-voltage-mv N` are both
  explicitly provided, lower-voltage descent does not stop solely because the
  loaded clock falls below `--auto-uv-max-clock-drop-pct`. Stability, load, FPS,
  crash, and final verification checks still apply.
- Efficiency tail tuning now respects an explicit tail-rise override, including
  `0`, while preserving the default balanced tail-tune behavior when no override
  is provided.

### Diagnostics

- Auto-UV now reports selected-GPU idle diagnostics sooner when Q2RTX appears
  to render on another GPU.
- Base-load and Q2RTX abort hints now point users toward explicit GPU selection
  on multi-GPU machines.

## PyPI Release Summary

PenguinBurner 0.2.1 adds explicit GPU selection, GUI/CLI tail-rise tuning, and
correct handling for explicit flat-tail low-voltage Auto-UV searches.

## Fedora COPR Release Summary

PenguinBurner 0.2.1 improves multi-GPU Auto-UV binding and adds explicit
tail-rise/min-voltage tuning behavior for advanced Auto-UV scans.
