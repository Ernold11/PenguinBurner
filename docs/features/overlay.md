# Performance Overlay

> Feature guide — see the [README](../../README.md) for the project overview.

The overlay draws a compact, lightweight, live performance readout on top of
your game, similar to a RivaTuner/MSI Afterburner on-screen display. Any tuning change you
make is reflected live in the overlay while you play, so you can see the effect
of an undervolt, clock, or fan change in real time without leaving the game.

![Overlay configuration tab](../assets/overlay.png)

## Enabling the overlay

The overlay launches with the game through a command wrapper. Copy the launch
string from the Overlay tab and paste it into Steam's launch options
(`PENGUIN_BURNER %command%`), or use the **Copy** button next to the field.

Top-level controls:

- **Enable overlay** — master toggle.
- **Update interval** — how often the readout refreshes (e.g. `1 s`).
- **Overlay scale** — size multiplier (`1x`, etc.).
- **Target FPS** — the reference frame rate, shared with
  [Adaptive Undervolting](./adaptive-uv.md).

The **Preview** line shows exactly what the on-screen string will look like, for
example:

```text
119 FPS  176 FG  LAT 23 ms  225 MHz  8 W  PERF  GPU 0%  CPU-T 98%
```

## Fields

Toggle each item independently. They are grouped into **Basic** and **Advanced**:

| Basic | Advanced |
| --- | --- |
| Base FPS | GPU % |
| FG FPS (frame-generation) | CPU % |
| Clock MHz | CPU-T (CPU temperature) |
| Voltage mV | Fan % |
| Power W | Temp C |
| Profile (tier: EFF/BAL/PERF) | Latency ms |
|  | UV offset mV |

## Pre-frame-generation FPS

The overlay shows two frame-rate numbers:

- **Base FPS** — the rendered present rate, before frame generation.
- **FG FPS** — the rate with frame generation, shown only when frame
  generation is active.

Having both makes it obvious how much of the displayed rate comes from generated
frames versus rendered ones.

## PC latency meter

The **LAT** field shows a full click-to-photon latency in milliseconds: the
render tail plus the present-to-scanout display tail. Both are on by default, so
you get the complete meter wherever the stack supports it. Where the display tail
isn't available, it falls back to render latency alone.

- Set `PENGUIN_BURNER_LATENCY_DISPLAY=0` to show render latency only.
- In-game latency is experimental on stock Proton (`--experimental-ingame-latency`).
