# PenguinBurner 0.2.4 Release Notes

## GitHub Release Notes

PenguinBurner 0.2.4 fixes Auto-UV startup on headless and SSH/X11-forwarded
NVIDIA systems where LACT can read live voltage but PenguinBurner rejected the
NVAPI voltage reader during preflight.

### NVAPI GPU Selection

- Hidden NVAPI voltage and V/F readers now match the selected GPU by PCI bus
  ID, following LACT's handle selection approach, instead of assuming NVAPI
  physical GPU order matches the selected NVML index.
- This avoids reading voltage from the wrong physical GPU on multi-GPU systems
  and systems where NVAPI enumeration order differs from NVML.

### Auto-UV Preflight

- An implausible idle-time NVAPI voltage sample is now reported as a warning
  with the raw sampled value instead of stopping Auto-UV immediately.
- Auto-UV continues into the workload phase and attempts to collect voltage
  telemetry again under load.

### SSH/X11 Forwarding

- The privileged GUI launcher now passes a default user `~/.Xauthority` path
  when X11 forwarding sets `DISPLAY` but does not export `XAUTHORITY`.

## PyPI Release Summary

PenguinBurner 0.2.4 fixes NVAPI GPU handle matching for live voltage reads,
allows Auto-UV to continue past implausible idle voltage samples, and improves
SSH/X11 forwarded GUI launches.
