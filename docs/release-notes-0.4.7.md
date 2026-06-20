# PenguinBurner 0.4.7

## Fixes

- **Q2RTX stability now uses PenguinBurner's headless benchmark binary** —
  the managed install downloads the PB Q2RTX release and extracts only the
  shareware data files from NVIDIA's archive. The old OpenSSL 1.1 compatibility
  staging, RUNPATH patching, RPM payload extraction, and display-wrapper
  fallback code are no longer part of the Q2RTX path.
- **Benchmark metrics come from the Q2RTX event pipe** — the stability runner
  uses the hot benchmark summary from the game itself, so the measured FPS window
  starts when the demo begins and no log-file loop parsing is needed.
