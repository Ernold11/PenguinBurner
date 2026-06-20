# Automatic Tuning (Auto-UV)

> Feature guide — see the [README](../../README.md) for the project overview and
> the [other feature pages](./) for adaptive UV, the overlay, profiles, the curve
> editors, and the silent fan curve.

Auto-UV is the core PenguinBurner feature. It tests your GPU under real load,
finds the most efficient stable voltage/frequency curve, and saves it for later
terminal or daemon use.

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
- Runs **Q2RTX** (PenguinBurner's headless Quake II RTX benchmark) plus a
  **CUDA** companion load for a real, GPU-bound workload. Q2RTX is downloaded
  automatically if missing.
- Walks voltage down step by step, verifying each candidate before accepting it.
- Stops before unsafe points, excessive clock loss, crashes, or NVIDIA Xid errors.
- Saves stable checkpoints as it goes, then runs a longer final verification
  (default `300s`) before publishing the curve.
- If you stop the scan after stable checkpoints exist, PenguinBurner offers those
  previously stable candidates for final verification instead of throwing the
  work away.

By default Q2RTX runs through PenguinBurner's managed
[headless benchmark binary](https://github.com/jpietek/Q2RTX-headless), so no
desktop display server or compositor wrapper is needed. Render resolution is
selected from the chosen GPU's NVML VRAM total: `2560x1440` for GPUs with
`<=8 GiB`, otherwise `3840x2160` when VRAM is larger or unavailable. Use
`--stability-width` / `--stability-height` to override it. Custom Q2RTX debug
switches are kept in the CLI advanced compatibility section.

## Stop, choose, or resume

You can stop Auto-UV from the GUI while it is scanning. After at least one stable
candidate exists, the stop request is handled as a controlled stop: PenguinBurner
opens the final-choice dialog with the already-passed candidates, and you can
choose which voltage/clock target should receive final verification. This does
not mark the current voltage unsafe. It only turns the completed checkpoints
into final-verification options.

If you stop before any stable checkpoint exists, there is no candidate to
verify, so the scan just stops.

Auto-UV writes an active-probe marker before each risky candidate or final
verification run. Normal exits, Ctrl-C, and SIGTERM remove that marker. If the
machine hangs, reboots, loses power, or the process is killed during a probe,
the next Auto-UV run consumes the stale marker, records that voltage/clock band
in `uv-result/auto-uv-unsafe-voltages.json`, and avoids repeating it.

When stable checkpoints exist for the same requested tier, the GUI shows a
previous-crash recovery dialog before starting discovery again. The default
choice is the next safer saved candidate above the failed voltage. Accepting a
candidate resumes from the saved baseline and candidate metrics, skips the
completed lower-voltage sweep, and goes straight to any remaining Performance
Auto-OC work plus final verification. Choosing **Start From Scratch** runs a new
scan instead, but the unsafe-voltage cache still applies.

Use `--fresh-auto-uv-scan` only when you deliberately want to forget the saved
Auto-UV state, including unsafe-voltage history and recovery candidates.

## Presets / tiers

![Auto-UV setup: GPU, preset, and Auto-OC targets](../assets/auto-uv-setup.png)

The performance-bias preset sets how much clock tail the curve keeps. These map
directly to [adaptive UV tiers](./adaptive-uv.md):

| Preset | Tail-rise bins | Extra |
| --- | --- | --- |
| Efficiency | `0` (flat) | lowest power |
| Balanced | `4` | moderate clock tail |
| Performance | `6` | adds an Auto-OC ladder (raises V+clock to targets) |

## GPU selection and telemetry

PenguinBurner reads GPU identity, PCI bus id, driver version, and VRAM directly
through NVML (`libnvidia-ml.so.1`). It does not shell out to `nvidia-smi` for the
GPU picker or Q2RTX resolution choice.

The selected `--gpu-index` is used consistently for NVML/NVAPI control,
telemetry, Q2RTX, CUDA, profile verification, and runtime profile application.
On multi-GPU systems, pick the card in the tuning dialog or pass `--gpu-index N`
so the benchmark and the curve writer target the same physical GPU.

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
