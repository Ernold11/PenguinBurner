# PenguinBurner 0.2.2 Release Notes

## GitHub Release Notes

PenguinBurner 0.2.2 improves the GUI Auto-UV start flow for multi-GPU systems
and makes the loaded-clock guardrail less brittle on newer NVIDIA cards.

### Auto-UV GPU Selection

- Added a GPU selector to the Auto-UV tuning modal. The dropdown lists detected
  NVIDIA GPUs as `GPU N - name (PCI bus)`, still shows the single card on
  one-GPU systems, and saves the selected index to `[gpu].index` before the scan
  starts.
- The selected GUI GPU index is passed into the Auto-UV scan and retained for
  follow-up profile verification and runtime actions.

### Auto-UV Defaults

- The default `--auto-uv-max-clock-drop-pct` is now derived from the detected
  GPU table's Efficiency-to-Performance clock ratio when available. Unknown
  GPUs use a generic fallback.
- The derived loaded-clock floor is applied to the selected GPU's measured
  baseline clock, so multi-GPU systems and different GPU families get a more
  appropriate default guardrail.
- Probe summaries now record run-to-run FPS variance so Efficiency stopping can
  require more confirmation on noisier timedemo runs.

## PyPI Release Summary

PenguinBurner 0.2.2 adds the Auto-UV GUI GPU selector and improves default
loaded-clock guardrails using selected-GPU table-derived clock-drop defaults.

## Fedora COPR Release Summary

PenguinBurner 0.2.2 improves GUI GPU selection for Auto-UV and derives default
loaded-clock drop guardrails from the selected GPU family when available.
