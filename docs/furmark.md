# FurMark Power Checks

This page records local FurMark comparison runs used to sanity-check
PenguinBurner runtime profiles under a heavy Vulkan load. These numbers are
host-specific, but the interpretation is useful when a profile appears to save
less board power than expected.

## Test command

From `/home/jp/furmark/furmark2/FurMark_linux64`:

```bash
./furmark \
  --demo furmark-vk \
  --gpu-index 0 \
  --max-time 30 \
  --width 3840 \
  --height 2160 \
  --fullscreen \
  --title-bar 0 \
  --vsync 0 \
  --no-score-box \
  --disable-demo-options
```

Power was read from `penguin-burnerd.service` journald telemetry lines, using
the `telemetry | ... power=...W` field emitted during the FurMark run window.

## 2026-07-03 4K Vulkan results

GPU: NVIDIA GeForce RTX 5080. FurMark renderer: Vulkan 1.4.341.

| Runtime profile | FurMark duration | Resolution | Frames | FPS min/avg/max | FurMark max temp | PenguinBurner samples | Max daemon power | At max power |
| --- | ---: | --- | ---: | --- | ---: | ---: | ---: | --- |
| Baseline / previous profile | `30005 ms` | `3840x2160` | `5202` | `161 / 173 / 180` | `74C` | `18` | `363.62 W` | `68C`, `2287 MHz`, `15001 MHz`, `880 mV` |
| Efficiency | `30005 ms` | `3840x2160` | `5175` | `159 / 172 / 179` | `68C` | `18` | `327.54 W` | `67C`, `2272 MHz`, `15001 MHz`, `825 mV` |

Delta from the first run to Efficiency:

- Max daemon power: `363.62 W -> 327.54 W`, down `36.08 W` (`9.9%`).
- Average FPS: `173 -> 172`, down `1 FPS` (`0.6%`).
- FurMark max temperature: `74C -> 68C`, down `6C`.

## Why the power drop is small

The Efficiency profile did lower the voltage seen at peak load, from the high
`870-880 mV` range to about `825-830 mV`, and the daemon status confirmed the
active profile as `Efficiency`. It did not substantially reduce loaded clocks:
the peak-power samples were still roughly `2287 MHz` before and `2272 MHz`
after, while memory stayed at `15001 MHz` in both runs.

That means the profile mostly improved voltage efficiency while preserving the
same FurMark throughput. FurMark is a sustained, board-power-heavy workload, so
the GPU can keep filling most of the available board-power envelope even after
the V/F curve is lowered. Board power also includes memory, fans, VRM losses,
and other card-side load, not only core voltage.

For a large wattage reduction under FurMark, use a real board power limit in
addition to the curve. Runtime profile tiers describe the V/F shape and
performance bias; they are not hard power caps.

## Measurement caveats

- The samples are 30-second runs with roughly 2-second daemon telemetry cadence.
- This is good for a quick before/after sanity check, not a statistically
  rigorous benchmark.
- For tighter A/B numbers, use longer runs, let the card cool to the same
  starting temperature, and record both max and average power.
