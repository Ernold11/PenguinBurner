# PenguinBurner Feature Guides

User documentation for each PenguinBurner feature, in priority order. See the
[project README](../../README.md) for the overview, install, and the LACT
comparison.

## Core

1. **[Automatic Tuning](./auto-uv.md)** — the Q2RTX + CUDA undervolt sweep that
   finds and verifies a stable, efficient V/F curve.
2. **[Adaptive Undervolting](./adaptive-uv.md)** — switch between tiered profiles
   at runtime based on frame-rate pacing.
3. **[Performance Overlay](./overlay.md)** — in-game FPS, pre-frame-gen FPS,
   PC latency meter, clocks, power, and active tier.

## Secondary

- **[Profile Management](./profile-management.md)** — apply, verify, tier,
  export, and clean up saved curves.
- **[Curve Editors](./curve-editor.md)** — manual V/F and fan curve editing.
- **[Silent Fan Curve](./silent-fan-curve.md)** — auto-generated quiet fan curve.

## Help

- **[Troubleshooting & FAQ](./troubleshooting.md)**

## Roadmap (planned, not shipped)

- Power limit control (GPU board power cap)
- Historical data plotting (power / clocks / FPS over time)
- Steam library discovery
- Per-game tuning profiles
