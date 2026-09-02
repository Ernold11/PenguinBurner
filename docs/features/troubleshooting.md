# Troubleshooting & FAQ

> See the [feature guides](./README.md) for how each part works.

### The scan stops early

Open the latest log in the PenguinBurner config directory's `debug-logs/`.
Common causes: an unsafe-voltage history entry, a clock guardrail, a Q2RTX or
CUDA failure, or interrupted final verification. To wipe history and rerun clean:

```bash
./penguin_burner.sh --fresh-auto-uv-scan
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

### Q2RTX installation ran out of disk space

Retry Auto-UV after freeing disk space. PenguinBurner detects an incomplete
managed Q2RTX install and rebuilds it automatically. To remove both the install
and any complete or partial cached downloads first, run this as your regular
user:

```bash
python3 -m stability.q2rtx --clean-q2rtx
```

The cleanup preserves PenguinBurner profiles, settings, scan history, and logs.
If the module command is unavailable in an older installation, use:

```bash
rm -rf -- "$HOME/.local/share/PenguinBurner/q2rtx" \
           "$HOME/.cache/PenguinBurner/q2rtx"
```

Then retry Auto-UV to download and install Q2RTX again. Do not use `sudo` for
either recovery command.

### Running on a headless server (no display)

Install the normal NVIDIA Vulkan driver stack. The managed Q2RTX benchmark path
uses the [headless Q2RTX fork](https://github.com/jpietek/Q2RTX-headless), so it
does not need Steam, a desktop display server, or a compositor wrapper.

### I have more than one GPU

Select the card with `--gpu-index N`, or pick it in the Auto-UV tuning dialog.
The Profiles tab's Target GPU selector filters saved profiles and controls
which card Apply, Restore defaults, and Apply on startup affect. Only the
currently active card has live monitoring, drift recovery, fan control, and
adaptive switching; other applied cards retain static curve, memory, and power
settings. When at least two NVIDIA GPUs are detected, **Main GPU** chooses the
saved startup card that owns monitoring after boot. Intel and AMD PRIME
adapters are not included in this NVIDIA/NVML selector.

For a boot-recovery issue, collect the daemon journal and its saved/replay
summary before changing the configuration:

```bash
journalctl -u penguin-burnerd.service -b --no-pager
python3 -m runtime.daemon_client boot-runtime-spec
python3 -m runtime.daemon_client status
```

The boot summary lists every saved GPU UUID and a replay outcome such as
`applied`, `active`, `stock-skipped`, `gpu-not-detected`, or `stock-fallback`.
This lets issue
reporters identify index changes, missing cards, and per-card recovery without
requiring a developer to reproduce the same hardware layout.

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
which requires root. That privileged work is done by a small root systemd
service, `penguin-burnerd` (a compiled Rust daemon), installed once with a
single admin prompt. The GUI, CLI, and Auto-UV scans themselves run as your
regular user and send requests to the service over a local socket — normal use
never asks for your password again.
