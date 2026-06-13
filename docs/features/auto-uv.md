# Automatic Tuning (Auto-UV)

> Feature guide — see the [README](../../README.md) for the project overview and
> the [other feature pages](./) for adaptive UV, the overlay, profiles, the curve
> editors, and the silent fan curve.

Auto-UV is the core PenguinBurner feature. It tests your GPU under real load,
finds the most efficient stable voltage/frequency curve, and saves it for later
foreground or daemon use.

![Auto-UV candidate sweep](../assets/auto-uv-scan.png)

The Auto-UV tab shows the live V/F scatter (base vs accepted candidate curve), a
streaming event log, and the per-step **Undervolting runs** table (mV, clocks,
FPS, power, temp, fan, FPS/W).

> ⚠️ Auto-UV makes real hardware changes. A bad voltage point can hang the GPU,
> crash the driver, or force a reboot. Auto-UV records each risky voltage before
> probing it and marks it unsafe after a crash, so later runs avoid it.

## Run a scan

```bash
sudo ./penguin_burner.sh                      # auto-starts a scan if no curve exists
sudo ./penguin_burner.sh --auto-uv-voltage-scan   # request it explicitly
sudo ./penguin_burner.sh --fresh-auto-uv-scan     # forget previous results, start clean
```

## What happens

- Resets clocks, offsets, power policy, and fan control before measuring.
- Runs **Q2RTX** (Quake II RTX timedemo) plus a **CUDA** companion load for a
  real, GPU-bound workload. Q2RTX is downloaded automatically if missing.
- Walks voltage down step by step, verifying each candidate before accepting it.
- Stops before unsafe points, excessive clock loss, crashes, or NVIDIA Xid errors.
- Saves stable checkpoints as it goes, then runs a longer final verification
  (default `600s`) before publishing the curve.

By default Q2RTX renders hidden via `gamescope --backend headless` (falls back
to an off-screen window). Use `--show-q2rtx-window` to watch it.

## Presets / tiers

![Auto-UV setup: GPU, preset, and Auto-OC targets](../assets/auto-uv-setup.png)

The performance-bias preset sets how much clock tail the curve keeps. These map
directly to [adaptive UV tiers](./adaptive-uv.md):

| Preset | Tail-rise bins | Extra |
| --- | --- | --- |
| Efficiency | `0` (flat) | lowest power |
| Balanced | `4` | moderate clock tail |
| Performance | `6` | adds an Auto-OC ladder (raises V+clock to targets) |

## Useful flags

| Flag | Purpose |
| --- | --- |
| `--auto-uv-max-drop-pct N` | voltage search depth below start (default `10`) |
| `--auto-uv-min-voltage-mv N` | explicit lowest voltage bin |
| `--auto-uv-max-clock-drop-pct N` | allowed loaded-clock loss (default: GPU Eco-to-Max ratio, else `12.5`) |
| `--auto-uv-tail-rise-bins N` | bins above lock point that may rise (`0` = flat) |
| `--auto-oc-target-voltage-mv N` / `--auto-oc-target-clock-mhz N` | Performance Auto-OC ceilings |
| `--gpu-index N` | select one NVIDIA GPU on multi-GPU systems |
| `--auto-uv-final-seconds N` | final verification duration |

The GUI exposes the GPU choice and Auto-OC targets in the tuning modal.

## After the scan

Runtime and daemon mode prefer the saved curve automatically:

```bash
sudo ./penguin_burner.sh --daemonize                  # apply saved curve
sudo ./penguin_burner.sh --daemonize --silent-fan-curve   # also apply quiet fan curve
```

Export a saved curve to [LACT](https://github.com/ilya-zlobintsev/LACT):

```bash
sudo ./penguin_burner.sh --export-lact-config lact-config.yaml \
  --auto-uv-profile latest \
  --lact-gpu-id "10DE:2704-1462:5110-0000:09:00.0"
```

`--auto-uv-profile` accepts a profile id, JSON path, `active`, or `latest`. Add
`--silent-fan-curve` to include fan settings. Review the file, then:

```bash
sudo install -m 0644 lact-config.yaml /etc/lact/config.yaml
sudo systemctl restart lactd
```

To run an imported Afterburner curve instead:
`--daemonize --prefer-afterburner-curve`.

## State and logs

Under the PenguinBurner user config directory:

- `debug-logs/` — per-scan stdout/stderr
- `uv-result/` — scan results and the unsafe-voltage crash cache
- `auto-uv-profiles/` — final profiles shown in the GUI

## Troubleshooting

If a scan stops early, read the latest log in `debug-logs/` first — common
causes are an unsafe-voltage history entry, a clock guardrail, a Q2RTX/CUDA
failure, or a short `--auto-uv-final-seconds`. To wipe history and rerun clean:

```bash
sudo ./penguin_burner.sh --fresh-auto-uv-scan
```

This keeps Afterburner imports and the Q2RTX download intact.
