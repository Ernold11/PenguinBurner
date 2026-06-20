# PenguinBurner 0.1.6 Release Notes

## GitHub Release Notes

PenguinBurner 0.1.6 focuses on Auto-UV guardrails, better final candidate
selection, and safer performance-mode limits.

### Highlights

- Added a borrowed GPU voltage/frequency table for RTX 50 and RTX 40 families.
- Auto-filled minimum voltage now uses the detected GPU's Efficiency table
  voltage as the lower sweep boundary, with a generic 10% fallback for
  unsupported GPUs.
- Performance-mode limits now use the GPU's Performance table voltage as the
  ceiling. For RTX 5080, performance probing is capped at 925 mV.
- Final candidate choice now follows the selected mode: Efficiency sorts by
  FPS/W, Performance sorts by FPS.
- The final candidate modal shows relative FPS and FPS/W deltas against the
  baseline.
- If a user stops Auto-UV after stable candidates exist, the UI now offers those
  candidates for final verification instead of only marking the run stopped.
- The Auto-UV tuning modal keeps the max-voltage-drop note short and
  user-facing, ending with the detected GPU name.

### Auto-UV Behavior

- The borrowed voltage/frequency table is used as a guardrail, not as a forced
  clock target.
- Performance probing uses measured baseline and lower-voltage clocks; the top
  endpoint is bounded by the borrowed Performance table voltage/clock.
- Performance-mode voltage/clock probing is capped by the table Performance
  voltage and no longer climbs toward the table Max voltage.
- Final verification uses the selected curve directly.
- Reference performance findings are documented separately, with direct-mode
  V-droop compensation kept out of production core logic.

### Packaging And Local Testing

- Package metadata is prepared for version 0.1.6.
- Local wheel and source distributions should pass `twine check`.
- Ubuntu PPA upload is intentionally left for a host with the Launchpad signing
  key.
- COPR upload requires local COPR credentials and `copr-cli`.

## PyPI Release Summary

PenguinBurner 0.1.6 adds Auto-UV GPU table guardrails, RTX 5080 performance
performance probing capped at 925 mV, mode-correct final candidate sorting, stopped
scan candidate selection, and clearer Auto-UV tuning modal text.
