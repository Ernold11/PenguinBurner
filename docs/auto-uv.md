# Auto-UV Quick Guide

Auto-UV is the normal setup path when you do not already have a trusted MSI
Afterburner undervolt profile. It runs in the foreground, finds a stable NVIDIA
voltage/frequency curve on Linux, and saves that curve for later foreground or
daemon runtime.

Undervolting is experimental hardware tuning. A bad voltage point can hang the
GPU, crash the driver, freeze the display, or force a reboot. Auto-UV records
the voltage being tested before each risky probe; after a crash or reboot, the
next run marks that voltage unsafe and avoids that voltage and lower voltages
unless you deliberately clear Auto-UV state.

## Start From Scratch

For a clean first run, use:

```bash
sudo ./penguin_burner.sh
```

If there is no saved Auto-UV curve and no usable imported Afterburner profile,
PenguinBurner starts the Auto-UV scan automatically.

To force a fresh scan and forget previous Auto-UV results:

```bash
sudo ./penguin_burner.sh --fresh-auto-uv-scan
```

You can also request the scan explicitly:

```bash
sudo ./penguin_burner.sh --auto-uv-voltage-scan
```

Auto-UV uses the readable candidate-sweep engine by default.

## What Happens During The Scan

- PenguinBurner resets GPU clocks, memory offsets, power policy, and V/F offsets before measuring.
- It runs Q2RTX plus a CUDA companion load to measure real loaded behavior. Q2RTX uses the freely available Quake II shareware timedemo with NVIDIA's Vulkan ray tracing renderer.
- Q2RTX renders at a real GPU-bound size. PenguinBurner uses `gamescope --backend headless` when available so Q2RTX does not create a visible desktop window. If gamescope is unavailable, it falls back to moving the real Vulkan window off-screen. Use `--show-q2rtx-window` if you want to see it.
- If Q2RTX is missing, PenguinBurner downloads and installs the managed copy automatically.
- It walks voltage down step by step, testing each candidate before accepting it.
- Balanced and Efficiency stop after the undervolt pass and final verification.
- Performance runs the same undervolt pass, then a separate Auto-OC ladder that raises voltage and clock proportionally up to the configured targets.
- It stops before unsafe points, severe clock loss, crashes, CUDA failures, Q2RTX failures, or NVIDIA Xid errors.
- It saves stable checkpoints while scanning, so progress is not lost if something fails.
- It runs a longer final verification before publishing the final curve.

The final verification default is `600s`.

## Headless Q2RTX

Auto-UV tries to keep Q2RTX hidden by default. When `gamescope` is installed,
PenguinBurner launches Q2RTX inside:

```text
gamescope --backend headless
```

Q2RTX still renders a real Vulkan workload at the configured size, normally
`2560x1440`. The render target is inside gamescope's private headless
compositor, so KDE, GNOME, X11, or Wayland do not get a visible Q2RTX window.

If gamescope is unavailable, PenguinBurner falls back to a best-effort
off-screen X11 window. That fallback can still be visible on some Wayland
desktops.

To show the Q2RTX window for debugging:

```bash
sudo ./penguin_burner.sh --auto-uv-voltage-scan --show-q2rtx-window
```

## How Auto-UV Decides To Continue Or Stop

PenguinBurner compares each accepted voltage step with the previous stable step.
The important values are the measured loaded voltage, temperature-normalized
power, and temperature-normalized FPS per watt.

- If requested voltage went down but measured loaded voltage did not go down, PenguinBurner assumes the NVIDIA driver ignored that step and keeps probing lower.
- If measured loaded voltage went down, temperature-normalized power went up, and temperature-normalized FPS per watt did not improve by at least `1.0%`, PenguinBurner treats that as a regression/no-gain step.
- By default, Auto-UV requires two confirmed regression/no-gain steps before FPS/W can stop the scan. To require a different confirmation count, use `--auto-uv-efficiency-stop-streak`.
- Auto-UV will not stop early from FPS/W regression/no-gain until it has scanned at least `10%` below the starting voltage by default. To change that floor, use `--auto-uv-min-efficiency-stop-drop-pct`.
- The loaded core-clock floor remains a safety guardrail; Auto-UV does not force the scan down to that floor just to stop on marginal FPS/W gains.
- If the next probe improves again, the stop streak is cleared and scanning continues.

The UI exposes this through presets: Efficiency uses `0` tail bins, Balanced
uses `4` tail bins, and Performance uses `6` tail bins. Performance also shows
editable Auto-OC voltage and clock targets. Advanced CLI users can override
those same targets with `--auto-oc-target-voltage-mv` and
`--auto-oc-target-clock-mhz`.

Auto-OC is intentionally separate from the lower-voltage sweep. It builds up to
10 proportional voltage/clock interpolation steps by default, probes each legal
step, keeps exploring after ordinary measured-clock/FPS regressions, and selects
the passing candidate with the best measured effective Q2RTX clock before the
single final verification.

Measured voltage is read through the Linux NVAPI voltage query automatically.
There is no opt-in flag. If voltage telemetry is unavailable on a driver or GPU,
PenguinBurner prints `n/a` and relies on the remaining safety checks.

The default efficiency guardrail allows up to a `10%` loaded GPU core clock
drop. If you want a looser efficiency clock-drop allowance, for example `12%`, run:

```bash
sudo ./penguin_burner.sh --auto-uv-max-clock-drop-pct 12
```

The three main aggressiveness options are:

- `--auto-uv-max-drop-pct N`: voltage search depth below the starting voltage;
  fallback default `10.0` when no GPU table floor or explicit mV floor exists.
- `--auto-uv-min-voltage-mv N`: explicit lowest voltage bin Auto-UV may try;
  overrides the GPU table floor.
- `--auto-uv-max-clock-drop-pct N`: allowed loaded-clock loss; default `10.0`.
- `--auto-uv-tail-rise-bins N`: number of V/F bins above the lock point that
  may rise after the flattened target. A value of `0` keeps the post-lock curve
  flat. If you explicitly combine `--auto-uv-tail-rise-bins 0` with an explicit
  `--auto-uv-min-voltage-mv N`, Auto-UV treats the lower-voltage pass as a
  user-requested floor search and does not stop that descent only because the
  loaded clock falls below `--auto-uv-max-clock-drop-pct`. Stability, load, FPS,
  crash, and final verification checks still apply.
- `--auto-oc-target-voltage-mv N`: Performance Auto-OC voltage ceiling.
- `--auto-oc-target-clock-mhz N`: Performance Auto-OC clock ceiling.
- `--gpu-index N`: select one NVIDIA GPU for the full Auto-UV lifecycle,
  including control, telemetry, Q2RTX render binding, CUDA load, verification,
  and runtime profile application. Use this on multi-GPU systems when the
  display-attached card is not GPU `0`.

Performance example:

```bash
sudo ./penguin_burner.sh --auto-uv-voltage-scan \
  --auto-uv-min-voltage-mv 850 \
  --auto-uv-max-clock-drop-pct 10 \
  --auto-oc-target-voltage-mv 925 \
  --auto-oc-target-clock-mhz 2670
```

Explicit flat-tail floor-search example for the second NVIDIA GPU:

```bash
sudo ./penguin_burner.sh --auto-uv-voltage-scan \
  --gpu-index 1 \
  --auto-uv-min-voltage-mv 800 \
  --auto-uv-tail-rise-bins 0
```

## After The Scan

Normal runtime and daemon mode prefer the saved Auto-UV curve automatically:

```bash
sudo ./penguin_burner.sh --daemonize
```

The saved V/F curve is stored in PenguinBurner's user config directory.

To export the saved V/F and fan curves as a complete Nvidia-only LACT config:

```bash
lact cli list-gpus
sudo ./penguin_burner.sh --export-lact-config lact-config.yaml \
  --lact-gpu-id "10DE:2704-1462:5110-0000:09:00.0"
```

To export a validated Afterburner profile instead:

```bash
sudo ./penguin_burner.sh --export-lact-config lact-config.yaml \
  --lact-source afterburner \
  --afterburner-dir "$AFTERBURNER_ROOT" \
  --section Profile1 \
  --lact-gpu-id "10DE:2704-1462:5110-0000:09:00.0"
```

The default LACT export writes only the V/F curve. Add `--silent-fan-curve` to
include fan settings too. Add `--fan-curve-export` only when you want fan
settings without replacing LACT's V/F curve.

Review the generated file before replacing LACT's config:

```bash
sudo install -m 0644 lact-config.yaml /etc/lact/config.yaml
sudo systemctl restart lactd
```

If you deliberately want to test an imported Afterburner curve instead, use:

```bash
sudo ./penguin_burner.sh --daemonize --prefer-afterburner-curve
```

## Fan Curve

Auto-UV may write a suggested quiet fan curve after final verification. It is
stored in PenguinBurner's user config directory.

PenguinBurner does not apply that fan curve by default. To opt in during normal
runtime or daemon mode:

```bash
sudo ./penguin_burner.sh --daemonize --silent-fan-curve
```

Fan-curve generation is blocked if the final load temperature is already too hot
for a quiet curve. The safety target is `75C` by default.

## Logs And Saved State

Every Auto-UV scan writes an attachable stdout/stderr log under:

```text
PenguinBurner user config directory / debug-logs
```

Auto-UV result and crash-cache files live under:

```text
PenguinBurner user config directory / uv-result
```

Final profiles shown by the GUI live under:

```text
PenguinBurner user config directory / auto-uv-profiles
```

If a voltage fails or the machine crashes during a probe, Auto-UV records that
voltage as unsafe and avoids that voltage and lower voltages on later scans.

## Troubleshooting

If a scan stops too early, check the latest Auto-UV stdout log first. Common
reasons are an unsafe-voltage history entry, a clock guardrail failure, a Q2RTX
failure, a CUDA failure, or an intentionally short `--auto-uv-final-seconds`
override.

To remove old Auto-UV history and rerun clean:

```bash
sudo ./penguin_burner.sh --fresh-auto-uv-scan
```

This cleanup keeps Afterburner imports and Q2RTX downloads intact.
