# Troubleshooting & FAQ

> See the [feature guides](./README.md) for how each part works.

### The scan stops early

Open the latest log in the PenguinBurner config directory's `debug-logs/`.
Common causes: an unsafe-voltage history entry, a clock guardrail, a Q2RTX or
CUDA failure, or interrupted final verification. To wipe history and rerun clean:

```bash
sudo ./penguin_burner.sh --fresh-auto-uv-scan
```

### I stopped Auto-UV before it finished

If at least one stable checkpoint exists, a controlled stop opens the final
choice dialog. Pick one of the previously stable voltage/clock candidates to run
final verification, or discard it and start over. A controlled stop is not added
to the unsafe-voltage cache.

### A Q2RTX window appears

The managed Q2RTX benchmark binary is headless and should not create an X11 or
Wayland window. If a window appears, check that the managed PenguinBurner Q2RTX
install is being used.

### Running on a headless server (no display)

Install the normal NVIDIA Vulkan driver stack. The managed Q2RTX benchmark path
uses the [headless Q2RTX fork](https://github.com/jpietek/Q2RTX-headless), so it
does not need Steam, a desktop display server, or a compositor wrapper.

### I have more than one GPU

Select the card with `--gpu-index N`, or pick it in the Auto-UV tuning dialog.

### Adaptive switching isn't doing anything

Adaptive needs at least two profiles with different tiers assigned. With one
tier it just runs that profile. Assign tiers from the Profiles tab or with
`--assign-auto-uv-tier`.

### The `penguin-burner` command isn't found

Make sure `~/.local/bin` is on your `PATH`.

### Voltage shows `n/a`

Voltage telemetry isn't available on that driver/GPU. The scan still runs using
its other safety checks. Use a recent driver and a supported card (RTX 30 / 40 /
50).

### Why does it need root?

Applying a curve changes real hardware (power limits, V/F offsets, fan control),
which requires root.
