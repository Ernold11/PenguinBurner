# NV-UV Reverse Engineering Findings

This note records what we learned from the local NV-UV binaries and which parts
are safe to borrow for Penguin Burner.

## What We Should Reverse

Reverse or leave reverted:

- Adaptive V-droop compensation in the scan loop. NV-UV mutates a direct-mode
  compensation value from measured loaded voltage and clamps it to 0-25 mV.
  That is coupled to its private NVAPI direct writer and should not be copied
  into our normal core logic yet.
- NVAPI direct strict-lock curve writing. NV-UV writes private VF control tables,
  folds points above the lock point to a low penalty clock, and retries batch
  writes. This is not a portable runtime behavior for us.
- NV-UV's broader downward optimize search. It probes down in 5 mV steps to
  `max(700, originalVoltageMV - 100)`, which is a separate policy from our
  current scanner and was the kind of core algorithm change that made results
  worse.
- Any recovery above the borrowed Performance preset voltage. In performance
  mode, the recovery ladder may seek FPS only up to the table's Performance
  voltage for that GPU, not the Max preset.

Keep:

- The borrowed GPU voltage/frequency preset table.
- The UI sorting fixes: Performance sorts by FPS, Efficiency sorts by FPS/W,
  with relative FPS and FPS/W deltas shown to the user.
- The max-voltage-drop auto-fill from the borrowed Eco voltage floor.
- The modal note as one short line ending with the GPU name.
- The performance-mode recovery voltage ceiling from the borrowed Performance
  preset voltage.

## Borrowed Table Policy

Use the table as bounds, not as a replacement scan algorithm.
The current `auto_uv3/scan_mode/uv_limits.py` table was rechecked against
NV-UV's `UVPresetService` IL and all 11 GPU-family tier values match.

- Eco preset voltage: lower sweep boundary for the automatic max voltage drop.
- Performance preset voltage: upper voltage recovery ceiling in performance
  mode.
- Max preset voltage: informational only for now; do not use it for automatic
  recovery.

For RTX 5080 the table says:

| Tier | Voltage | Clock |
| --- | ---: | ---: |
| Eco | 850 mV | 2800 MHz |
| Balanced | 900 mV | 2800 MHz |
| Performance | 925 mV | 2980 MHz |
| Max | 975 mV | 3150 MHz |

So RTX 5080 performance-mode voltage recovery must stop at 925 mV.

## Modal Copy Rule

Before starting Auto OC, the scan tuning modal may show only a short note:

```text
Max voltage drop auto-filled for NVIDIA GeForce RTX 5080
```

Do not mention "efficiency floor" in the modal. The technical reason can live in
the tooltip or docs, not in the visible note.

## Evidence From NV-UV

The local `NV-UV.exe` is a .NET single-file bundle. The bundle contained
`NVUV.Core.dll`, `NV-UV.dll`, `NV-UV.r2r.dll`, dependency metadata, and runtime
config. The core service type is `NVUV.Core.Services.AutoUVService`.

Current local evidence source:

- `/home/jp/nvuv/NV-UV.exe`
- `/home/jp/nvuv/NvApiNative.dll`
- `/home/jp/nvuv/NV-UV_Tester_Guide_EN.html`
- `/home/jp/nvuv/nvuv.txt`

The .NET bundle manifest is readable from the EXE footer. Relevant extracted
payloads:

| Payload | Bundle offset | Size | Evidence |
| --- | ---: | ---: | --- |
| `NVUV.Core.dll` | `58,957,824` | `1,052,672` | managed core services and algorithms |
| `NV-UV.dll` | `60,010,496` | `1,261,568` | WPF app/view-model layer |
| `NV-UV.r2r.dll` | `120,565,760` | `98,758,656` | ready-to-run app image |
| `NvApiNative.dll` | `9,852,416` | `234,063` | private NVAPI bridge |
| `NV-UV.deps.json` | `219,324,416` | `43,212` | dependency graph |
| `NV-UV.runtimeconfig.json` | `10,086,479` | `526` | .NET runtime config |

Relevant managed behavior found in `NVUV.Core.dll`:

- `TryEnterDirectMode` uses NVAPI direct mode when it can read the stock VF
  curve; otherwise it falls back to the Afterburner path.
- `ApplyViaNvapi(freqMHz, voltageMV, applyComp)` chooses a VF point within
  about 15 mV of the requested voltage plus any active V-droop compensation.
  It writes a strict lock at that point and pushes higher points to a penalty
  clock.
- `AdaptVDroopCompFromSamples(requestedMv, samples)` computes loaded median
  voltage and adjusts compensation by the difference, clamped to 0-25 mV.
- Load-qualified voltage and clock sampling ignores the first 5 seconds, keeps
  samples at or above 60% of max observed power, requires at least 5 qualified
  samples, and uses medians.
- `RunVoltageSearchInline` first tries a pre-resolved mapping if present, then
  probes downward in 5 mV steps until instability, crash, cancellation, or the
  voltage floor.

Relevant native bridge behavior found in `NvApiNative.dll`:

- Exports include `NvApiDirect_Init`, `ReadCurve`, `ReadOffsets`, `SetPoint`,
  `SetBatch`, `ResetAll`, and `GetLastError`.
- `SetBatch` writes active point offsets through a private NVAPI VF control call,
  verifies offsets, retries, and falls back to per-point writes when needed.
- The native code clamps offsets to +/-1,000,000 kHz before writing.

## Managed Algorithm Map

The useful managed metadata is not obfuscated. Important method evidence from
`NVUV.Core.dll`:

| Type | Methods |
| --- | --- |
| `NVUV.Core.Services.AutoUVService` | `ApplyViaNvapi`, `AdaptVDroopCompFromSamples`, `TryEnterDirectMode`, `ComputeLoadQualifiedClock`, `ComputeLoadQualifiedVoltage`, `RunAutoUV`, `RunSingleProbe`, `RunVoltageSearchInline`, `PersistOptimizeRunGroup`, `RestartWorkerIfCrashed`, `ForceRespawnWorker` |
| `NVUV.Core.Services.RenderStressEngine` | `RunStepTest`, `RenderFrameValidation`, `DispatchComputeStress`, `FmaBurst`, `DispatchFma`, `ReadbackFmaErrors`, `ApplyHeartbeatFrame`, `ConfirmPixelFailure`, `CheckDeviceRemoved` |
| `NVUV.Core.Services.GameReplayService` | `OnDriverCrashDetected`, `OnPreCrashWarningDetected`, persistent downstep adjustments |
| `NVUV.Core.Interop.NvApiDirect` | `Init`, `ReadStockCurve`, `ReadOffsets`, `SetPoint`, `SetBatch`, `ResetAll` |
| `NVUV.Core.Algorithms.PresetDatabase` | GPU preset lookup and stock-curve fallback tables |

Observed Auto-UV flow:

1. Ensure Afterburner/profile state and stock curve are available. NV-UV also
   tracks `CoreClkBoost` calibration and falls back to preset database curves
   when profile-derived stock data is missing or contaminated.
2. Start or reuse the render-stress worker. The worker owns the D3D12/DXR/FMA
   stress device so a device removal or TDR can be isolated and the worker can
   be respawned.
3. Prefer NVAPI Direct Mode for scanner writes when `NvApiNative.dll` can read
   the stock curve. Otherwise fall back to Afterburner profile writes.
4. Apply one requested voltage/frequency point, run the render stress test, and
   collect telemetry samples.
5. Qualify loaded samples by ignoring the first 5 seconds, then requiring power
   at or above 60% of the max observed post-warmup power. At least 5 qualified
   samples are required.
6. Use load-qualified medians for the scanner's clock and voltage decisions.
   For clock, it also computes a P90-style clock from the sorted qualified
   values.
7. If Direct Mode V-step compensation is enabled, adapt the compensation from
   `requestedMv - loadedMedianVoltageMv`, clamped through the current
   compensation range of 0-25 mV.
8. `RunVoltageSearchInline` first tries a pre-resolved mapped voltage when one
   exists. If that passes and the measured median clock reaches the requested
   frequency, that mapped voltage is accepted.
9. Without a mapping, it searches downward in 5 mV steps from
   `originalVoltageMV - 5` to `max(700, originalVoltageMV - 100)`.
10. Each probe is recorded into the search result. On unstable/crash/cancel it
    stops and preserves the last good probe as the fallback.
11. Optimize runs persist the whole probe group before the picker dialog so a
    TDR or WPF failure does not lose the measured points.
12. Game Replay/WHEA handling is separate from the scanner. It stores pending
    downsteps immediately when a hard crash or pre-crash warning is detected.

## Direct Mode And V-Droop Details

`ApplyViaNvapi` does not write an Afterburner-style full curve. It builds a
strict-lock NVAPI batch:

- Effective voltage is `requestedVoltageMV + currentVDroopCompMV`, but only
  when per-scan compensation is enabled.
- It prefers the first stock VF point at or above the effective voltage. If
  that point is more than 15 mV away, it searches the nearest VF point and still
  rejects the write when the nearest point is more than 15 mV away.
- If the requested target frequency exactly matches the stock frequency at the
  chosen point, it writes one MHz below stock to force a non-zero batch offset.
- The selected lock point gets the target offset.
- Points below the selected voltage are left inactive/zeroed.
- Points above the selected voltage are pushed to a penalty clock derived from
  `max(500, selectedStockFrequencyMHz - 200)`.
- It writes arrays of up to 128 point offsets/active flags via
  `NvApiDirect.SetBatch`.

`AdaptVDroopCompFromSamples` is deliberately small:

```text
if not Direct Mode: return
if compensation disabled: log skip and return
(medianVoltage, sampleCount) = ComputeLoadQualifiedVoltage(samples)
if medianVoltage <= 0: log skip and return
delta = requestedMv - medianVoltage
newComp = clamp(currentComp + delta, 0, 25)
currentComp = newComp
```

This is a measured-voltage correction loop, not just a hardcoded +5 mV rule.
The release notes describe common UI/history cases as Comp 0 or Comp +5, but
the IL allows any accumulated integer value from 0 through 25 mV.

Do not confuse that adaptive loop with the WPF `V-Step Compensation` control in
`NV-UV.dll`. `MainViewModel.GetVStepCompensatedVoltage(targetVoltageMV,
compSteps)` filters stock VF voltages to valid bins (`> 0` and `< 4000` mV),
sorts distinct voltages above the requested target, and picks the `compSteps`th
higher bin. If there are not enough higher bins it uses the highest available
higher bin, and if none exist it falls back to the requested target voltage.
The `VStepEnabled` setter observed in the UI stores enabled as a single positive
step, and Direct Apply paths can bypass it. This is a bin-selection helper, not
the same thing as `AdaptVDroopCompFromSamples`.

## Preset And Stock-Curve Algorithm

NV-UV has two preset layers:

- `UVPresetService` is the UI tier table used for Eco/Balanced/Performance/Max
  cards and warnings.
- `PresetDatabase` is a GPU/variant lookup used for stock-curve and CCB
  fallback when profile data is unavailable or suspect.

`UVPresetService.GetTiersForGpu` uppercases the GPU name and matches substrings
in this order: `4090`, `4080`, `4070 TI SUPER`/`4070TI SUPER`, `4070 TI`/
`4070TI`, `4070 SUPER`, `4070`, `5070 TI`/`5070TI`, `5070` without `TI`,
`5080`, `5060 TI`/`5060TI`, and `5060`. If no branch matches, it falls back to
the RTX 5090 tier table. The service-level max preset voltage helper returns
`1050` mV.

`UVPresetService.Calculate` does not blindly trust a target clock. For each
preset tier, it:

1. Finds the stock VF point nearest to the tier target voltage.
2. Reads that stock frequency as `StockAtVoltage`.
3. Snaps the tier target clock to NV-UV's frequency step helper.
4. Runs a driver-limit validation at the tier voltage.
5. If the requested clock is outside the driver limit, snaps down to
   `MaxAtVoltage`.
6. Stores the resulting offset as `ActualFreqMHz - StockAtVoltage`.

`PresetDatabase.FindGpu` uppercases the GPU name and matches configured GPU
patterns by substring. `FindVariant` uses `SubVendorId` when it is available,
otherwise it returns the first variant for that GPU. `GetCcbForGpu` falls back
to `-200` MHz when the GPU is unknown. The managed metadata exposes stock-curve
fallback methods for Zotac AMP RTX 5090, Asus RTX 5080, and Asus RTX 5070 Ti.

## Game Replay And Crash Handling

Game Replay is separate from the Auto-UV scan. Its default settings are:

| Setting | Default |
| --- | ---: |
| Strategy | `FreqDown` |
| Frequency step | `50` MHz |
| Voltage step | `10` mV |
| Minimum frequency | `1500` MHz |
| Maximum voltage | `1050` mV |

The strategy enum is `FreqDown`, `VoltUp`, or `Both`. On a driver crash,
`GameReplayService.OnDriverCrashDetected` requires an active profile/frequency/
voltage, fires a crash-imminent callback, enforces a 15 second crash cooldown,
captures telemetry when available, and then computes a new pair:

- `FreqDown`: lower frequency by `FreqStepMHz`.
- `VoltUp`: raise voltage by `VoltageStepMV`.
- `Both`: lower frequency and raise voltage together.

It clamps against the minimum frequency and maximum voltage. If both clamps
would leave the point unchanged, it logs that limits were reached and does not
persist a new adjustment. Otherwise it updates the active frequency/voltage,
persists a per-process `GameReplayAdjustment`, increments crash counters, and
fires an immediate hard-crash persistence callback for D3D device removal and
GPU page fault crash types.

Pre-crash handling is similar but faster. `OnPreCrashWarningDetected` is driven
by WHEA or `nvlddmkm` warnings, uses a 3 second cooldown, applies the same
strategy/clamps, and persists the adjustment without waiting for a full TDR.

`CrashLogService.RecordCrash` stores game/process, tier, preset id, reason,
detection method, telemetry snapshot, resolution bracket, GPU name, and the
downgraded tier. It tracks crashed tiers and consecutive crashes per game.
`GetRecommendedTier` steps down if the current or newly recommended tier has
crashed before; three consecutive crashes force tier `C`.

`DriverCrashMonitor` classifies crash events into `TDR`, `DriverHung`,
`DriverFailed`, `D3DDeviceRemoved`, `GpuPageFault`, `WheaCorrected`,
`NvlddmkmWarning`, and `Unknown`. WHEA corrected events become pre-crash
warnings; selected `nvlddmkm` warning event ids also become pre-crash warnings.

## Render Stress Algorithm

`RenderStressEngine.ResolveGpuStressProfile` chooses a workload profile from
the power limit:

| Tier | Power limit | Base/max post-process passes | Target power factor | Scene weights |
| --- | ---: | ---: | ---: | --- |
| `S` | `>= 500 W` | `4`/`24` | `SP.TdpFactor` | Pixel 0.50, DXR 0.15, Raster 0.15, Vdroop 0.10, Idle 0.05, Cycle 0.05 |
| `A+` | `>= 400 W` | `6`/`20` | `0.93` | Pixel 0.60, DXR 0.10, Raster 0.15, Vdroop 0.08, Idle 0.02, Cycle 0.05 |
| `A` | `>= 280 W` | `11`/`16` | `0.93` | Pixel 0.77, DXR 0.05, Raster 0.16, Vdroop 0.02, Idle 0.00, Cycle 0.00 |
| `B` | `>= 150 W` | `8`/`12` | `0.90` | Pixel 0.60, DXR 0.08, Raster 0.12, Vdroop 0.10, Idle 0.05, Cycle 0.05 |
| `C` | fallback | `10`/`10` | `0.88` | Pixel 0.65, DXR 0.05, Raster 0.10, Vdroop 0.10, Idle 0.05, Cycle 0.05 |

The heartbeat action enum is `RasterFrame`, `RasterHeavy`, `PixelSustained`,
`DxrFrame`, `IdleGap`, `VdroopKiller`, `MicroSleep`, and `PowerCycle`. The
heartbeat loop randomly chooses weighted phases, changes post-process passes,
DXR draw multipliers, compute dispatch counts, viewport size, and short sleep
intervals. `VdroopKiller` creates bursts of 5-10 frames with high ray count and
minimal compute to provoke transient voltage behavior. `RunStepTest` considers
steps shorter than 5 seconds invalid, reports FMA math errors and confirmed
pixel failures, and supports frequency downsteps during the step.

## Better Than Penguin Burner

- Direct scanner writes are much faster than profile-file writes. NV-UV claims
  roughly 50 ms point writes in Direct Mode.
- Load-qualified medians are stronger than plain averages for deciding whether
  the requested voltage was actually held under load.
- V-droop compensation explicitly measures driver/GPU undershoot at each step.
  Penguin Burner currently records loaded voltage, but does not feed an
  adaptive compensation loop.
- Optimize group persistence is robust. Failed/crashed probe endpoints are
  written before the user picker, not only after a final choice.
- The render worker and hard-crash downstep persistence are good operational
  patterns for surviving TDR/device-removal failures.
- WHEA/pre-crash monitoring gives NV-UV a signal before a full driver reset on
  Windows.
- Its custom workload mixes DXR, compute, FMA checking, heartbeat load changes,
  and power cycling. That is good at finding transient instability that a single
  steady benchmark can miss.
- Game Replay's per-game downstep persistence is practical: it records a safer
  point immediately after a crash, instead of relying on the user to remember
  what failed.

## Worse Than Penguin Burner

- NV-UV depends on Windows, Afterburner profile state, and private NVAPI direct
  calls. Penguin Burner stays closer to Linux/NVML/LACT-compatible behavior.
- Direct Mode strict-lock writes are not the same curve shape as the final
  persisted Afterburner profile. The scanner can therefore test a different
  runtime surface from the saved profile.
- Pushing higher voltage points to a penalty clock is useful for forcing a lock,
  but it can hide behavior that would occur on a normal flattened or ramped
  curve.
- Adaptive V-droop compensation can make the displayed/requested voltage look
  lower than the actual voltage needed to hold the point. That is useful for
  measurement, but risky as a default policy.
- The downward voltage search floor `max(700, original - 100)` is broad and not
  mode-aware. It can chase aggressive low-voltage points beyond the conservative
  table guardrails we now use.
- The GPU-name matcher falls back to the RTX 5090 table for unknown GPUs. That
  is acceptable for a UI recommendation card, but too risky for automatic
  Linux-side voltage limits.
- `V-Step Compensation` in the UI is labeled like a millivolt offset but behaves
  as a stock-VF-bin step. That can confuse users and logs.
- The voltage search accepts a point primarily from median clock reaching the
  requested frequency. Penguin Burner's candidate choice also considers FPS,
  FPS/W, frame-count consistency, CUDA/Q2RTX errors, power/load evidence, and
  final long verification.
- NV-UV's newest Ada support is explicitly experimental/community based.

## Penguin Burner Improvement Backlog

Borrow cautiously:

1. Done locally: add analysis-only loaded medians to
   `AutoUvProbeSummary`: median loaded voltage, median loaded clock, P90
   loaded clock, and qualified sample count.
   Keep current pass/fail behavior unchanged at first.
2. Done locally: show/report observed V-droop as telemetry:
   `requested_mv - median_loaded_mv`. This stays diagnostic and does not
   become compensation.
3. Persist complete probe groups for a scan attempt, including failed and
   unsafe endpoints, before final-choice UI. Our latest verified candidate file
   is not the same as NV-UV's full optimize-group history.
4. Consider a post-candidate micro-search around the selected final point:
   +/- one or two editable voltage bins, with the same final verification gate.
   This borrows NV-UV's bidirectional search idea without adopting its broad
   `original - 100 mV` floor.
5. Evaluate whether median loaded voltage/clock should replace mean values for
   recovery decisions after the diagnostic fields have enough real logs.
6. Keep Direct Mode V-droop compensation out of production until there is a
   separate backend, explicit user opt-in, and hardware crash recovery strategy.
7. Borrow the crash-replay concept only as local Linux crash-cache metadata:
   persist "this game/profile crashed at this point" and propose a safer point
   next run. Do not auto-apply a recovery profile without an explicit user gate.
8. Consider adding short idle-gap/power-cycle phases to the companion stress
   workload. This is safer to borrow than private NVAPI direct writes because it
   improves instability detection without changing curve application semantics.
9. If a future V-step helper exists, label it as "next VF bin" compensation, not
   a raw millivolt offset.

Do not borrow yet:

- Private NVAPI batch writes as a default Linux runtime path.
- Automatic compensation that silently raises voltage above the user's selected
  candidate.
- The broad downward optimize search as the main algorithm.
- Unknown-GPU fallback to a high-end preset table.

## Practical Decision

For Penguin Burner, the useful part is the preset table as a conservative guard
rail:

- Auto-fill max voltage drop from the Eco floor.
- In performance mode, recover voltage upward only through the Performance
  voltage ceiling for the detected GPU.
- Keep the existing scanner's stability rules and candidate selection policy.

The NV-UV direct-mode and adaptive V-droop ideas are worth keeping as research
notes, but they should stay out of production until we can test them as an
explicit backend with separate safety gates.
