# Troubleshooting & FAQ

> See the [feature guides](./README.md) for how each part works.

### The scan stops early

Open the latest log in the PenguinBurner config directory's `debug-logs/`.
Common causes: an unsafe-voltage history entry, a clock guardrail, a Q2RTX or
CUDA failure, or a short `--auto-uv-final-seconds`. To wipe history and rerun
clean:

```bash
sudo ./penguin_burner.sh --fresh-auto-uv-scan
```

### A Q2RTX window appears

The scan runs Q2RTX hidden via `gamescope --backend headless`. Without
gamescope it falls back to an off-screen window, which can still show on some
Wayland desktops. Install gamescope to keep it hidden. To watch it on purpose,
add `--show-q2rtx-window`.

### Running on a headless server (no display)

Install `gamescope` first. It is the only path that runs Q2RTX with no display
server at all (`gamescope --backend headless` uses its own private offscreen
compositor). The off-screen X11 fallback is not headless — it needs a live
`$DISPLAY`, so on a display-less server the run fails to create a Vulkan
surface. `gamescope` does pull in Wayland/wlroots/Xwayland libraries, but they
are inert files and start no desktop or window. A lighter Wayland-free Xvfb
path is planned; see `docs/dev/q2rtx-headless-xvfb-plan.md`.

### I have more than one GPU

Select the card with `--gpu-index N`, or pick it in the Auto-UV tuning dialog.

### Adaptive switching isn't doing anything

Adaptive needs at least two profiles with different tiers assigned. With one
tier it just runs that profile. Assign tiers from the Profiles tab.

### The `penguin-burner` command isn't found

Make sure `~/.local/bin` is on your `PATH`.

### Voltage shows `n/a`

Voltage telemetry isn't available on that driver/GPU. The scan still runs using
its other safety checks. Use a recent driver and a supported card (RTX 30 / 40 /
50).

### Why does it need root?

Applying a curve changes real hardware (power limits, V/F offsets, fan control),
which requires root.
