<p align="center">
  <img src="docs/assets/penguin-burner-logo.png" alt="PenguinBurner logo" width="160">
</p>

# PenguinBurner CLI

PenguinBurner is an NVIDIA Auto-UV tuning tool. The default app entrypoints,
`penguin-burner` and `pburn`, start the Qt GUI. The explicit CLI entrypoints,
`penguin-burner-cli` and `pburn-cli`, are for Auto-UV scans, profile
verification, and applying saved Auto-UV profiles as daemon runtime.

## Install

```bash
python -m pip install --user --upgrade penguin-burner
```

Start the GUI:

```bash
~/.local/bin/penguin-burner
```

Run the CLI from an installed package:

```bash
sudo ~/.local/bin/penguin-burner-cli --auto-uv-voltage-scan
```

From a checkout, use the wrapper:

```bash
sudo ./penguin_burner.sh --auto-uv-voltage-scan
```

## Auto-UV Scan

Scans are explicit because they make hardware changes. Start one with:

```bash
sudo ./penguin_burner.sh --auto-uv-voltage-scan
```

The CLI scan options mirror the GUI Auto-UV tuning dialog:

- `--gpu-index N`: select the NVIDIA GPU used for scan, verification, and runtime.
- `--auto-uv-mode efficiency|balanced|performance`: select the same preset family as the GUI.
- `--auto-uv-min-voltage-mv N`: Efficiency min-voltage floor.
- `--auto-uv-max-clock-drop-pct N`: max loaded core-clock drop allowed.
- `--auto-uv-memory-offset-mhz N`: memory clock V/F offset saved with the profile.
- `--auto-uv-power-limit-w N`: power limit applied during the scan and saved with the profile.
- `--auto-uv-tail-rise-bins N`: preset tail-rise value passed by the GUI.
- `--auto-oc-target-voltage-mv N`: Performance Auto-OC voltage target.
- `--auto-oc-target-clock-mhz N`: Performance Auto-OC clock target.

Examples:

Balanced with GUI defaults:

```bash
sudo ./penguin_burner.sh --auto-uv-voltage-scan --auto-uv-mode balanced
```

Balanced with the default tail-rise value made explicit:

```bash
sudo ./penguin_burner.sh --auto-uv-voltage-scan \
  --auto-uv-mode balanced \
  --auto-uv-tail-rise-bins 4
```

Efficiency with explicit GUI knobs:

```bash
sudo ./penguin_burner.sh --auto-uv-voltage-scan \
  --auto-uv-mode efficiency \
  --auto-uv-min-voltage-mv 850 \
  --auto-uv-max-clock-drop-pct 10 \
  --auto-uv-memory-offset-mhz 500 \
  --auto-uv-power-limit-w 390
```

Performance using the detected GPU table Auto-OC target:

```bash
sudo ./penguin_burner.sh --auto-uv-voltage-scan \
  --auto-uv-mode performance
```

Performance with a custom Auto-OC target:

```bash
sudo ./penguin_burner.sh --auto-uv-voltage-scan \
  --auto-uv-mode performance \
  --auto-oc-target-voltage-mv 910 \
  --auto-oc-target-clock-mhz 2950 \
  --auto-uv-tail-rise-bins 6
```

If only one Performance Auto-OC target flag is supplied, the missing voltage or
clock value comes from the detected GPU table target. Unknown GPUs need both
custom target values for the Auto-OC ladder.

The scan stays attached to the terminal because it is actively testing voltage
stability. If the system crashes during a probe, PenguinBurner records the
in-progress voltage as unsafe on the next run and avoids that voltage unless
Auto-UV state is deliberately cleared.

## Profiles And Runtime

List saved Auto-UV profiles:

```bash
./penguin_burner.sh --list-auto-uv-profiles
```

Apply the latest saved Auto-UV profile as a daemon after a final curve exists:

```bash
sudo ./penguin_burner.sh --daemonize --auto-uv-profile latest
```

Install the latest verified Auto-UV profile as the persistent boot-time service:

```bash
sudo ./penguin_burner.sh --install-systemd-service --auto-uv-profile latest
```

Install it with the saved silent fan curve too:

```bash
sudo ./penguin_burner.sh --install-systemd-service --auto-uv-profile latest --silent-fan-curve
```

Remove the persistent boot-time service:

```bash
sudo ./penguin_burner.sh --uninstall-systemd-service
```

By default daemon runtime applies the saved V/F curve and leaves fan control to
the GPU driver. Add `--silent-fan-curve` to opt into PenguinBurner's saved
Auto-UV fan curve:

```bash
sudo ./penguin_burner.sh --daemonize --auto-uv-profile latest --silent-fan-curve
```

Adaptive Auto-UV can switch between saved verified profile tiers:

```bash
sudo ./penguin_burner.sh --daemonize --adaptive-auto-uv
```

For persistent adaptive boot autostart:

```bash
sudo ./penguin_burner.sh --install-systemd-service --adaptive-auto-uv
```

Generated Efficiency, Balanced, and Performance scans are tiered automatically.
To override existing saved profiles, copy ids from `--list-auto-uv-profiles` and
assign them explicitly:

```bash
./penguin_burner.sh --assign-auto-uv-tier <eff-profile-id> efficiency
./penguin_burner.sh --assign-auto-uv-tier <bal-profile-id> balanced
./penguin_burner.sh --assign-auto-uv-tier <perf-profile-id> performance
```

Remove a manual tier assignment:

```bash
./penguin_burner.sh --assign-auto-uv-tier <profile-id> none
```

## Overlay And Latency

For Steam games, the intended visible launch option is:

```text
PENGUIN_BURNER %command%
```

Enable the native in-game overlay for games launched through the wrapper:

```text
PENGUIN_BURNER_OVERLAY=1 PENGUIN_BURNER %command%
```

Enable optional in-game latency marker parsing:

```text
PB_INGAME_LATENCY=1 PENGUIN_BURNER %command%
```

Enable verbose latency diagnostics in the daemon log:

```text
PENGUIN_BURNER_DUMP_LATENCY_DATA=1 PENGUIN_BURNER %command%
```

## Debugging

Write a diagnostic log for the current operation:

```bash
sudo ./penguin_burner.sh --debug-log --auto-uv-voltage-scan
```

Follow daemon logs:

```bash
sudo journalctl -u PenguinBurner.service --since "-4 hours" -f
```

More details:

- [Auto-UV guide](docs/features/auto-uv.md)
- [Overlay guide](docs/features/overlay.md)
