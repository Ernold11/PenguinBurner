# PenguinBurner 0.4.6

## New

- **`PENGUIN_BURNER_DUMP_LATENCY_DATA=1` (advanced)** — an opt-in daemon
  environment variable that dumps verbose latency internals to
  the daemon log: swapchain **present mode** and **queue depth**, plus Reflex
  **sleep-mode (boost / FPS-cap)** and recovery transitions. Off by default;
  for debugging display/VRR and frame-generation behaviour. Reflex sleep-mode
  lines are logged on change only, so the per-frame set calls do not flood the
  log.

## Docs

- New **Advanced / hacky flags** section in the README documenting the
  power-user CLI flags and latency-layer environment variables.
- Document latency-layer launch ordering and display-latency findings.

## Fixes

- Update the packaging-metadata test to match the merged AUR build change (cmake
  is a system build dependency, not a Python build-system requirement).
