<p align="center">
  <img src="docs/assets/penguin-burner-logo.png" alt="PenguinBurner logo" width="160">
</p>

# NVIDIA GPU Automatic Tuning Tool

PenguinBurner is an NVIDIA GPU automatic undervolting tool. It helps you visualize and manage your GPU fine tuning setup with single voltage/frequency bin precision to maximize FPS per Watt, potentially leading into +33% and higher improvements for recent cards. MSI Afterburner imports and LACT exports are also supported.

The main mode is **Auto-UV**:
PenguinBurner tests your Nvidia GPU under load, finds a stable undervolt, and
saves it for normal terminal or daemon runtime. MSI Afterburner import is
still available as an optional fallback path.

GPU undervolting is meant to make your graphics card consume significantly less
power while giving up as little performance as possible. The practical result
can be dead-silent fan operation, lower temperatures, and lower electricity
bills. PenguinBurner automatically searches for the operating sweet spot of your
Nvidia GPU, so you do not have to resort to trial and error or risk introducing
avoidable system instability.

Having issues with PenguinBurner? Please report bugs on the
[GitHub Issues page](https://github.com/jpietek/PenguinBurner/issues).

## Quick Start From PyPI

Install the published package:

```bash
python -m pip install --user --upgrade penguin-burner
```

Start the GUI:

```bash
~/.local/bin/penguin-burner
```

Before Auto-UV starts, the GUI shows a GPU dropdown in the tuning modal. It
lists detected NVIDIA cards as `GPU N - name (PCI bus)`, shows the single card
when only one is present, and saves the selected index to `[gpu].index`.

The short alias starts the same GUI:

```bash
~/.local/bin/pburn
```

For CLI/runtime usage, use the explicit CLI command:

```bash
sudo ~/.local/bin/penguin-burner-cli --daemonize
```

## Auto-UV Quick Start

You do not need an MSI Afterburner profile. From a local checkout, start
PenguinBurner with no arguments:

```bash
sudo ./penguin_burner.sh
```

If no saved Auto-UV curve exists, PenguinBurner automatically starts the
terminal Auto-UV scan. Keep it attached while it searches; this is
intentional because it is testing voltage stability.

![Auto-UV result summary](docs/assets/auto_uv_result_summary_terminal.png)

Example Auto-UV result summary: before/after power draw, core clock, and efficiency shown after the final verification.

What Auto-UV does:

1. Reset previous GPU tuning so the scan starts from the driver/default curve.
2. Install the managed Q2RTX test workload automatically if it is missing. Q2RTX uses the freely available Quake II shareware demo data with PenguinBurner's headless Vulkan ray tracing benchmark binary.
3. Measure the base loaded behavior: clock, voltage, power, temperature, fan speed, and FPS/W.
4. Pick a safe target core clock and start lowering voltage one real V/F bin at a time.
5. Test each candidate with Q2RTX plus CUDA load.
6. In Performance mode, run a separate Auto-OC ladder after the undervolt pass.
7. Accept a candidate only if it stays stable, keeps enough loaded core clock,
   keeps FPS within a `10%` floor of the previous stable probe, and does not
   collapse into idle or low-load telemetry.
8. Keep scanning lower while power/efficiency still makes sense; stop when lower voltage no longer helps, a guardrail fails, or an unsafe point is reached.
9. Run a final long verification, `300s` by default. If you stop the scan after stable checkpoints exist, PenguinBurner can offer those previously stable candidates so you can choose which target gets final verification. If the long check fails, Auto-UV backs off to a safer voltage/curve and reruns the long verification; the final result is published only after the long verification completes.
10. Return the GPU to the driver/default curve before the scan exits. The saved curve is applied later by runtime or daemon mode.

By default Auto-UV uses the GPU table's Eco-to-Max clock ratio as the loaded
core-clock drop allowance. Unknown GPUs use `12.5%`. To force a `12%`
clock-drop allowance:

```bash
sudo ./penguin_burner.sh --auto-uv-max-clock-drop-pct 12
```

Undervolting can hang the GPU, crash the driver, freeze the display, or force a
reboot. If the system crashes during an Auto-UV probe, PenguinBurner records the
in-progress voltage as unsafe on the next run and will not test that voltage or
lower voltages again unless you deliberately clear Auto-UV state.

Stopping Auto-UV normally is different from crashing. After at least one stable
checkpoint exists, a controlled stop can open the final-choice dialog and let you
select one of the already-passed candidates for final verification. It does not
mark the current voltage unsafe.

After the scan, daemonize normal runtime:

```bash
sudo ./penguin_burner.sh --daemonize
```

By default runtime applies the saved V/F curve and leaves fan control to the GPU
driver. To opt into the saved Auto-UV fan curve too:

```bash
sudo ./penguin_burner.sh --daemonize --silent-fan-curve
```

Saved Auto-UV files are written under PenguinBurner's user config directory:

- final V/F curve
- suggested fan curve
- debug logs

More details: [Auto-UV quick guide](docs/features/auto-uv.md).

To export the saved Auto-UV V/F and fan curves as a complete Nvidia-only LACT
config:

```bash
lact cli list-gpus
sudo ./penguin_burner.sh --export-lact-config lact-config.yaml \
  --lact-gpu-id "10DE:2704-1462:5110-0000:09:00.0"
```

To export a specific saved Auto-UV profile instead of the latest profile:

```bash
./penguin_burner.sh --list-auto-uv-profiles
sudo ./penguin_burner.sh --export-lact-config lact-config.yaml \
  --auto-uv-profile 900mv-2100mhz \
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

By default the LACT export writes only the V/F curve. Add `--silent-fan-curve`
when you also want PenguinBurner's fan curve in the LACT config. To export only
fan settings and leave LACT's V/F curve untouched:

```bash
sudo ./penguin_burner.sh --export-lact-config lact-fan-config.yaml \
  --fan-curve-export \
  --lact-gpu-id "10DE:2704-1462:5110-0000:09:00.0"
```

Review the file, then install it deliberately if it matches your LACT GPU id:

```bash
sudo install -m 0644 lact-config.yaml /etc/lact/config.yaml
sudo systemctl restart lactd
```

![LACT imported Auto-UV curve](docs/assets/lact_auto_uv_import.png)

Example LACT result after importing PenguinBurner's saved Auto-UV curve. The
active curve is the exported undervolt: lower voltage bins ramp normally, then
the tuned load range flattens at the verified target clock while LACT keeps the
driver's original base curve visible underneath for comparison.

`--auto-uv-profile` accepts a profile id, candidate id, JSON path, `active`, or
`latest`. PenguinBurner clamps exported Nvidia V/F clocks to LACT's default
positive offset ceiling of `base_mhz + 1000` MHz and logs a warning if any point
is reduced. Use `--lact-max-vf-offset-mhz N` only if your LACT/NVML setup allows
a different maximum offset.

If you already have a known-good MSI Afterburner profile, you can still
preview/import it with:

```bash
./penguin_burner.sh --dry-run --afterburner-dir "$AFTERBURNER_ROOT"
```

![Dry-run preview](docs/assets/dry_run_curves_terminal.png)

Example dry-run preview: imported V/F target and fan curve rendered directly in the terminal before any GPU changes are applied.

PenguinBurner is experimental NVIDIA tuning software. It is intended for modern
Linux NVIDIA drivers, but GPU generation and driver behavior can differ.
Requires NVIDIA driver libraries for runtime control, telemetry, and GPU profile auto-selection.
Reverse engineering, profile parsing, and import support were developed against MSI Afterburner `4.6.6.16757`.
Other MSI Afterburner versions are not guaranteed to work.

## Runtime Requirements

- `python3` must be available in `PATH`, `3.11+` is required
- no third-party Python packages are required; PenguinBurner uses only the Python standard library
- NVIDIA driver libraries such as `libnvidia-ml.so.1` and `libnvidia-api.so.1` are required at runtime, but they come from the NVIDIA driver

## Python Package

From a local checkout, install PenguinBurner with:

```bash
python -m pip install .
```

For an editable development install:

```bash
python -m pip install -e .
```

That installs GUI commands `penguin-burner` and `pburn`, plus compatibility
aliases `penguin-burner-ui` and `pburn-ui`. CLI/runtime commands are
`penguin-burner-cli` and `pburn-cli`.

After a release is published to PyPI, users can install it with:

```bash
python -m pip install penguin-burner
```

The PyPI package name uses the correct spelling: `penguin-burner`. The GitHub
Actions release workflow builds the package and checks the metadata when a
GitHub release is published. Publish to PyPI deliberately with configured
twine credentials, and do not commit or paste PyPI API tokens.

The GitHub release tag must match `project.version` in `pyproject.toml`, either
as `X.Y.Z` or `vX.Y.Z`. The publish workflow rejects mismatches before upload,
so GitHub releases, tags, and the PyPI version stay in sync.

To build and check a release locally before creating the GitHub release:

```bash
python -m pip install --upgrade build twine
rm -rf dist/
python -m build
python -m twine check dist/*
```

## Quality Checks

Current local release checks:

- `pytest`: mocked tests for Auto-UV logic, profile management, UI command
  construction, manual curve editing, packaging metadata, Afterburner parsing,
  LACT export, and Q2RTX helpers
- local `sdist` and wheel build with `python -m build`
- package metadata validation with `python -m twine check dist/*`
- spot-check that the built source distribution includes README screenshots and
  that the wheel metadata embeds the current README

## Acknowledgements

Additional thanks:

- NVIDIA: For the amazing graphics technology, that unfortunately lacks some features and polish on Linux.
- Qt Project: For the excellent Qt6 UI.
- Codex GPT 5.5 (Open AI): This project was implemented fully with AI based on human driven ideas and reverse engineering.

Special thanks to the LACT project and to Ilya Zlobintsev for pushing Linux NVIDIA tuning forward.

While PenguinBurner was still reverse engineering proprietary NVIDIA binaries and had only working voltage getters, LACT landed a working custom voltage/frequency point setter first. In particular, LACT pull request [`#957`](https://github.com/ilya-zlobintsev/LACT/pull/957), `feat: add Nvidia VF curve editor`, was merged on April 18, 2026.

If any part of PenguinBurner's MSI Afterburner profile parsing or import logic is useful to LACT, feel free to borrow it.

## Proprietary inputs are not bundled

This repository does not ship MSI Afterburner binaries or copied profile exports.

If you want to import Afterburner data, point PenguinBurner at the real MSI
Afterburner directory from Windows. By default that directory is:

- `C:\Program Files (x86)\MSI Afterburner`

On Linux, `--afterburner-dir` should point at that same directory through a
mounted Windows drive or a copied directory tree. PenguinBurner expects this
layout under that root:

- `MSIAfterburner.cfg`
- `Profiles/*.cfg`

or set `PENGUIN_BURNER_AFTERBURNER_ROOT` to another directory with that layout.

## CLI Options

Auto-UV does not require an MSI Afterburner profile. If no final Auto-UV curve
exists yet and no usable Afterburner import exists, running with no arguments
starts the foreground scan:

```bash
sudo ./penguin_burner.sh
```

The explicit scan command is also available:

```bash
sudo ./penguin_burner.sh --auto-uv-voltage-scan
```

### Auto-UV Options

- `--auto-uv-voltage-scan`: start the foreground Auto-UV voltage scan explicitly.
- `--list-auto-uv-profiles`: list saved Auto-UV profiles and exit; use a shown profile id or candidate id with `--auto-uv-profile`.
- `--install-q2rtx`: download the managed Q2RTX workload without starting a scan.
- `--export-lact-config PATH`: write a complete Nvidia-only LACT `config.yaml` from the saved Auto-UV V/F curve; add `--auto-uv-profile PROFILE` to select a profile and `--silent-fan-curve` to include fan settings.
- `--lact-source auto-uv|afterburner`: choose the LACT export source; default `auto-uv`. The `afterburner` source uses the same Afterburner selection flags as `--dry-run`.
- `--fan-curve-export`: with `--export-lact-config`, export only fan settings and omit LACT's `gpu_vf_curve`.
- `--lact-gpu-id ID`: LACT GPU id for `--export-lact-config`; get it from `lact cli list-gpus`.
- `--lact-max-vf-offset-mhz N`: maximum positive Nvidia V/F offset to emit for LACT; default `1000`, with exported clocks clamped to `base_mhz + N`.
- `--auto-uv-final-seconds N`: final verification duration after the best curve is selected; default `300`.
- `--auto-uv-short-seconds N`: base Q2RTX verification length; default `10` seconds, allowed range `10..60`. Medium and deep voltage tiers use 2x and 2.5x this value; the CUDA companion uses a longer slot in the deep tier.
- `--auto-uv-max-clock-drop-pct N`: maximum loaded core-clock drop allowed during scan; default is the detected GPU table's Eco-to-Max clock ratio, or `12.5` for unknown GPUs.
- `--auto-oc-target-voltage-mv N`: Performance Auto-OC voltage ceiling.
- `--auto-oc-target-clock-mhz N`: Performance Auto-OC clock ceiling.
- `--auto-uv-min-voltage-mv N`: lowest voltage bin Auto-UV may try; overrides the detected GPU table floor.
- `--auto-uv-tail-rise-bins N`: number of V/F bins above the lock point that may rise after the flattened target; default depends on Auto-UV mode.
- `--auto-uv-max-drop-pct N`: fallback voltage search depth when no GPU table floor or explicit min voltage is available; default `10.0`.
- `--gpu-index N`: select the NVIDIA GPU index used for Auto-UV control, telemetry, Q2RTX, CUDA, and runtime actions. The Qt GPU dropdown lists detected indices from NVML directly; `nvidia-smi` is not used as the picker backend.
- `--stability-test`: run the Q2RTX plus CUDA stability workload directly and exit.
- `--stability-seconds N`: duration for `--stability-test`; default `300`.
- `--stability-width N` and `--stability-height N`: Q2RTX render size; defaults to VRAM-based auto selection (`2560x1440` for GPUs with `<=8 GiB`, `3840x2160` above that or when VRAM cannot be read).
- `--stability-log-dir PATH`: write `--stability-test` logs to a specific directory.
- `--check-latency-layer`: check Vulkan loader discovery for the opt-in PenguinBurner latency telemetry layer and print Steam launch options.

The normal runtime also opens a latency telemetry socket at
`$XDG_RUNTIME_DIR/penguin-burner/latency.sock` and logs
`event=latency-meter` summaries when the opt-in Vulkan layer sends samples.

#### Auto-UV Aggressiveness Tuning

The most important Auto-UV aggressiveness knobs are voltage search depth,
loaded-clock drop allowance, and Performance Auto-OC targets.

- `--auto-uv-min-voltage-mv N` controls the lowest voltage bin Auto-UV may try.
  The UI fills this from PenguinBurner's GPU table when the card is known. If
  the GPU is unknown, Auto-UV falls back to `--auto-uv-max-drop-pct 15`.
- `--auto-uv-max-drop-pct N` controls the fallback percentage depth below the
  starting voltage only when there is no explicit mV floor and no GPU table
  floor. Higher values search deeper and can find lower power points, but they
  also spend more time near crash-prone voltage bins.
- `--auto-uv-max-clock-drop-pct N` controls how much loaded core clock Auto-UV
  may sacrifice while lowering voltage. By default, known GPUs use the table
  Eco-to-Max clock ratio and unknown GPUs use `12.5%`. Lower values preserve
  more performance; higher values search deeper.
- `--auto-uv-tail-rise-bins N` controls the rising tail after the flattened
  lock point. Higher values preserve more upper-curve headroom; `0` keeps the
  post-lock curve flat. When both `--auto-uv-tail-rise-bins 0` and
  `--auto-uv-min-voltage-mv N` are explicitly provided, Auto-UV treats the
  lower-voltage descent as a user-requested floor search and does not stop that
  descent only because the loaded clock falls below
  `--auto-uv-max-clock-drop-pct`. Stability, load, FPS, crash, and final
  verification checks still apply.
- `--auto-oc-target-voltage-mv N` and `--auto-oc-target-clock-mhz N` override
  the Performance-mode Auto-OC endpoint. Performance first completes the same
  undervolt pass as Balanced, then probes a bounded proportional voltage/clock
  ladder up to those targets.

GPU selection:

- `--gpu-index N` binds the whole PenguinBurner lifecycle to one NVIDIA GPU:
  NVML/NVAPI control, telemetry, Q2RTX render launch, CUDA load, profile
  verification, and daemon/runtime actions all use that index. This matters on
  systems with multiple NVIDIA cards, where the display-attached GPU and the
  first NVML GPU may differ.
- The Qt GUI accepts the same selection at launch with
  `penguin-burner-ui --gpu-index N` or `penguin-burner-ui --index N`. If no GUI
  index is provided, the GUI reads `[gpu].index` from the runtime config. The
  Auto-UV tuning modal also includes a GPU dropdown and saves the selected GPU
  index before starting the scan.

Verification duration knobs:

- `--auto-uv-final-seconds N` controls final verification confidence. Shorter
  runs finish faster; longer runs are more trustworthy.
- `--auto-uv-short-seconds N` controls the base verification length.
  The default `10` means shallow, medium, and deep candidate probes use
  `10`, `20`, and `25` seconds of Q2RTX respectively. CUDA is skipped for
  shallow probes, then uses `5` seconds for medium and `10` seconds for deep.

Efficiency profile:

```bash
sudo ./penguin_burner.sh --auto-uv-voltage-scan \
  --auto-uv-min-voltage-mv 850 \
  --auto-uv-max-clock-drop-pct 10
```

Performance profile:

```bash
sudo ./penguin_burner.sh --auto-uv-voltage-scan \
  --auto-uv-min-voltage-mv 850 \
  --auto-uv-max-clock-drop-pct 10 \
  --auto-oc-target-voltage-mv 925 \
  --auto-oc-target-clock-mhz 2670
```

Example: use a looser `12%` core-clock drop allowance during Auto-UV:

```bash
sudo ./penguin_burner.sh --auto-uv-voltage-scan --auto-uv-max-clock-drop-pct 12
```

Example: explicitly sweep a flat-tail curve toward `800mV` on GPU index `1`:

```bash
sudo ./penguin_burner.sh --auto-uv-voltage-scan \
  --gpu-index 1 \
  --auto-uv-min-voltage-mv 800 \
  --auto-uv-tail-rise-bins 0
```

### Afterburner Import Options

- `--dry-run`: preview imported Afterburner V/F and fan curves without applying them.
- `--afterburner-dir PATH`: path to the MSI Afterburner directory.
- `--section NAME`: choose a saved Afterburner profile section, such as `Profile1`.
- `--afterburner-device-profile PATH`: choose the exact Afterburner `Profiles/*.cfg` device file.

Example dry run:

```bash
./penguin_burner.sh --dry-run --afterburner-dir "$AFTERBURNER_ROOT"
```

### Runtime And Daemon Options

- `--daemonize`: start PenguinBurner as a transient `systemd` service.
- `--install-systemd-service`: install the boot-time systemd service.
- `--uninstall-systemd-service`: remove the boot-time systemd service.
- `--silent-fan-curve`: opt into PenguinBurner manual fan-curve control during runtime/daemon mode.
- `--config PATH`: use a specific runtime config path.
- `--gpu-index N`: select a specific NVIDIA GPU.
- `--debug-log`: write a verbose diagnostic log for the current operation.
- `--journal-hours N`: change the suggested `journalctl --since` window shown after daemonizing.

Default Afterburner profile validation:

- PenguinBurner only auto-selects saved non-default manual V/F presets.
- By default, the selected preset must contain a flattened tail that can be turned into a lock point.
- That flattened lock point must be a real undervolt versus `Defaults` or `Startup` at the same clock, with at least `5mV` of margin.
- If no saved preset passes those checks, PenguinBurner stops instead of guessing.

## Runtime launch

- Use `penguin_burner.sh` as the single user entrypoint. It resolves the repo path itself, so you do not need to `cd` into the repository first.
- Running `penguin_burner.sh` directly stays attached to the terminal by default.
- On a clean first run, `sudo ./penguin_burner.sh` starts Auto-UV automatically. After a final Auto-UV curve exists, the same command runs normal foreground runtime using that saved curve.
- The checked-in `PenguinBurner.service` file is only an example. The preferred path is `sudo ./penguin_burner.sh --install-systemd-service`, which writes a unit with the real absolute script path for the current checkout.
- On the first interactive run with a newly configured Afterburner root, PenguinBurner automatically imports that root into its managed config, runs a dry-run preview, then prompts you to continue in the terminal or daemonize later.
- `--dry-run` is the recommended first step. It parses the selected Afterburner root directory, prints concise summaries, and draws console charts for the V/F curve and fan curve without attempting GPU writes. It does not require sudo.
- `--debug-log` can be combined with `--dry-run`, a first-time import, or terminal runtime testing when you need the full profile-discovery and parsing trail for an incompatible or otherwise unexpected MSI Afterburner export.
- the extra debug payload is written to the debug log file only; it does not spam stdout during terminal runs and it does not add extra noise to the `systemd` journal
- Actual runtime control and any real V/F, power-limit, persistence-mode, or optional fan changes should be treated as privileged operations and run with `sudo`.
- Normal runtime and daemon mode do not control fans unless `--silent-fan-curve` is present.
- Use `--daemonize` only when you explicitly want PenguinBurner to launch as a transient `systemd` service.
- Use `--install-systemd-service` only when you explicitly want a persistent boot-time `systemd` service.
- Use `--uninstall-systemd-service` to remove that persistent service again.
- If `systemd` is unavailable, `--daemonize` exits with a clear error instead of pretending to background itself.
- During direct terminal runs, logs go to stdout.
- In daemonized mode, logs go to the `systemd` journal, not to a hardcoded file in the repository.
- `--journal-hours N` changes the suggested `journalctl --since` window shown after daemonizing. The default view window is `4` hours.

Examples:

```bash
sudo ./penguin_burner.sh
```

```bash
sudo ./penguin_burner.sh --daemonize
```

```bash
sudo ./penguin_burner.sh --daemonize --silent-fan-curve
```

```bash
sudo ./penguin_burner.sh --install-systemd-service
```

```bash
./penguin_burner.sh --dry-run --afterburner-dir "$AFTERBURNER_ROOT"
```

```bash
./penguin_burner.sh --install-q2rtx
./penguin_burner.sh --stability-test --stability-seconds 300
```

```bash
sudo journalctl -u PenguinBurner.service --since "-4 hours" -f
```

## Q2RTX Stability Test

`--stability-test` runs a non-interactive Q2RTX benchmark workload and then exits.
It is intentionally isolated from the normal fan-control runtime path.

What it does:

- uses VRAM-based render settings by default: `2560x1440` for GPUs with `<=8 GiB`, `3840x2160` above that or when VRAM cannot be read
- launches PenguinBurner's [headless Q2RTX benchmark binary](https://github.com/jpietek/Q2RTX-headless) by default, so no desktop display server or compositor wrapper is needed
- disables V-Sync and dynamic resolution scaling, pins the dynamic-resolution min/max scale to `100%`, disables FSR upscaling, leaves the render loop uncapped, and keeps sound off
- forces heavy real-time RTX features on, including `pt_num_bounce_rays=2`, `pt_reflect_refract=8`, `pt_thick_glass=2`, `pt_caustics=1`, bloom, volumetric lighting, and the particle/beam/sprite paths
- asks the benchmark binary for the requested measured duration; measurement starts when the demo actually begins
- prefers a ready-made `q2demo1` demo from `pak0.pak` when shareware data is installed
- reads benchmark events through the event pipe for exact hot-window `frames / seconds / fps` metrics
- compares hot-window average FPS against the previous stable baseline so regressions above the configured floor fail the run
- polls lightweight NVIDIA telemetry through NVML
- records the Q2RTX stdout/stderr log to a file
- marks the run as failed if the benchmark exits early, if benchmark metrics are missing or invalid, if hot-window FPS drops too far versus baseline, if fatal output patterns appear, if the selected GPU is idle, or if NVIDIA Xid messages are detected after launch

### Headless Q2RTX

By default, Auto-UV and `--stability-test` run PenguinBurner's headless Q2RTX
benchmark binary. This is not a fake low-resolution render: it still creates
the configured Vulkan render target and runs the RTX workload, but it uses the
[headless Q2RTX fork](https://github.com/jpietek/Q2RTX-headless) and does not
need a desktop display server or compositor wrapper.

On a headless server, install the normal NVIDIA Vulkan driver stack and run the
same command. There is no compositor package to install for the Q2RTX benchmark
path.

What it does not do yet:

- it does not verify rendered pixels or detect subtle visual corruption
- it does not score image quality or compare frame hashes
- it is currently a practical "did the measured heavy benchmark window complete with sane FPS behavior and without obvious driver faults" check

If Q2RTX is not installed yet, run `./penguin_burner.sh --install-q2rtx`. That downloads PenguinBurner's latest headless Q2RTX release from GitHub, then downloads the official NVIDIA Q2RTX shareware archive only as a data source. PenguinBurner extracts the required `baseq2` demo data (`pak0.pak`, `blue_noise.pkz`, `q2rtx_media.pkz`, and `shaders.pkz`) and ignores NVIDIA's bundled executable and compatibility libraries. The Auto-UV path also performs this install automatically when the managed Q2RTX install is missing or older than the latest managed release, printing the download, extraction, and runtime-library progress to stdout. Steam is not used.

By default PenguinBurner auto-selects a ready-made demo, first looking for demos on disk and then inside `pak0.pak`, preferring demos like `q2demo1`.

## Auto-UV Voltage Scan

Auto-UV stays attached to the terminal while scanning. Start it, let it finish, then
daemonize normal runtime.

```bash
sudo ./penguin_burner.sh
```

The scan resets old clock offsets, measures real loaded behavior, tests lower
voltages one by one, and writes a final curve only after verification. It saves
stable checkpoints as it goes, so a controlled stop can still offer those
previously stable targets for final verification. It also remembers unsafe
voltages so a later run does not repeat a bad point.

After the scan, run normal runtime:

```bash
sudo ./penguin_burner.sh --daemonize
```

If you also want PenguinBurner to apply the saved fan curve during runtime:

```bash
sudo ./penguin_burner.sh --daemonize --silent-fan-curve
```

Detailed user guide: [Auto-UV quick guide](docs/features/auto-uv.md).

## Dry Run First

`--dry-run` is intended for experimentation. It is the safest way to verify that
PenguinBurner is parsing the right MSI Afterburner directory before you let it
touch live GPU state.

What dry-run shows:

- the selected Afterburner root, device profile, and section
- a compact V/F summary in `MHz` and `mV`
- an ASCII V/F chart with `mV` on the x-axis and `MHz` on the y-axis, overlaying target `#` against base `.`, with lock point `@`
- an ASCII fan chart with temperature on the x-axis and fan percent on the y-axis
- translated power-limit and memory-offset previews

What dry-run does not do:

- it does not enable persistence mode
- it does not set power limits
- it does not write V/F offsets
- it does not take over fan control

Suggested workflow:

1. Run `--dry-run` until the preview matches what you expect.
2. Try different saved sections or device profiles if needed.
3. Only then run the real terminal or `systemd` path with `sudo`.

If parsing fails, import behaves unexpectedly, or the wrong Afterburner profile is being selected, re-run with
`--debug-log`. That writes a timestamped file under
`debug-logs/` next to the selected config file; with the default config that is
the selected config file under `debug-logs/`. The log includes the discovered device
profiles, per-section validation results, raw section key dumps, V/F blob and
fan-curve blob metadata, per-point Linux V/F translation details, chosen fan
profile, terminal runtime diagnostics, and traceback details for parsing errors.

If something does not work with your MSI Afterburner export, please open an issue at:

- `https://github.com/jpietek/PenguinBurner/issues`

Attach the full debug log file or files, not just a pasted excerpt. That makes it much
easier to improve PenguinBurner for new profile variants and parsing failures.

If you have a useful bug fix, parser improvement, documentation cleanup, or any
other improvement that makes PenguinBurner work better, pull requests are
welcome too.

Examples:

```bash
./penguin_burner.sh --dry-run --afterburner-dir "$AFTERBURNER_ROOT"
```

```bash
./penguin_burner.sh --dry-run --afterburner-dir "$AFTERBURNER_ROOT" --section <saved-section>
```

```bash
./penguin_burner.sh --dry-run --afterburner-dir "$AFTERBURNER_ROOT" --debug-log
```

## Log fields

Example status line:

```text
temp=...C fan=...%/...% power=...W gpu_clock=...MHz mem_clock=...MHz voltage=...mV clk_ceiling=...->...MHz@...mV mem_vf_offset=...MHz vf_point=...MHz@...mV vf_offset=...MHz vf_base=...MHz@...mV uv=...mV fan_curve_state=hardware-auto next_fan_step=...C->...% fan_mode=auto
```

This example is the hardware-auto case. Without `--silent-fan-curve`, normal runtime lines include `fan_control=disabled` and PenguinBurner does not write fan speeds. Once PenguinBurner takes over the fans with `--silent-fan-curve`, steady-state lines switch to `fan_mode=manual` and also include `target`, `curve`, and `hyst`.

Some fields are conditional and only appear when the related policy or telemetry path is active.

- `temp`: current GPU temperature.
- `fan`: current reported fan speed for each fan, in percent.
- `power`: current board power draw in watts.
- `gpu_clock`: current graphics clock.
- `mem_clock`: current memory clock.
- `voltage`: current GPU voltage telemetry.
- `clk_ceiling`: the intended flattened V/F curve point, shown as `requested->applied`, with `@voltage` when present.
- `mem_vf_offset`: applied global memory V/F offset in MHz. This is an offset control, not the live memory clock reading.
- `vf_point`: current matched core V/F point from the live V/F table, shown as `clock@voltage`.
- `vf_offset`: current offset applied at that matched core V/F point. This is a curve-local MHz offset, not a direct undervolt value in mV.
- `vf_base`: base core V/F point used as the comparison reference.
- `uv`: voltage delta versus `vf_base`; a negative value means the current matched point is below the base reference voltage.
- `fan_curve_state`: current fan-control state. `hardware-auto` means the GPU's own automatic fan control still owns the fans; other lines can show the active manual curve segment or `emergency-auto`.
- `next_fan_step`: next fan-curve transition PenguinBurner is waiting for, shown as `temp->speed`, or `resume-custom`/`none` when that is the next action.
- `fan_control`: appears as `disabled` when PenguinBurner is intentionally leaving fans to the hardware/driver.
- `fan_mode`: overall fan-control mode for the current line when `--silent-fan-curve` is active. `auto` means the GPU's own automatic fan control still owns the fans. `manual` means PenguinBurner is actively setting fan speed. Transition/event lines such as entering or restoring manual mode may log an `event=` field instead of `fan_mode`.

## Run At Your Own Risk

Dry-run is encouraged. Real hardware changes are not.

Once you leave `--dry-run`, PenguinBurner can perform operations such as:

- enabling persistence mode
- setting board power limits
- writing core V/F offsets
- writing memory V/F offsets
- taking over fan control, but only when `--silent-fan-curve` is used

Those paths can hang the GPU, crash the driver, freeze the display, or require a
reboot. Treat them as experimental tuning operations.

Auto-UV intentionally stays attached while scanning because it is testing
crash-prone voltage points. During each risky voltage probe it writes an
active-probe marker and removes it during normal cleanup, including Ctrl-C and
SIGTERM. If the machine hangs, reboots, loses power, or the process is forcibly
killed during a probe, restart PenguinBurner normally. The stale marker is
treated as an abrupt previous probe end, that voltage is marked unsafe, and
future scans avoid that voltage and lower voltages unless Auto-UV state is
cleared.

The GUI Stop button is a controlled stop. Once Auto-UV has at least one stable
checkpoint, stopping opens the same final-choice path and lets you final-verify
one of the already-passed candidates. Controlled stops are not written as unsafe
voltage entries.

For actual fan or V/F curve changes, use `sudo`. If the preview is not exactly what you want, go back to `--dry-run` and keep iterating there.
