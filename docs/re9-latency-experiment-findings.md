# Resident Evil Requiem Latency Experiment Findings

Date: 2026-06-08

Steam app: `3764200`

Game path:

```text
/home/jp/.local/share/Steam/steamapps/common/RESIDENT EVIL requiem BIOHAZARD requiem/re9.exe
```

## Goal

Find a Linux/Proton telemetry source that exposes real GPU-bound render latency
for a Reflex-capable game, preferably the pre-DLSS-frame-generation latency that
the NVIDIA App overlay shows on Windows.

The visible FPS or generated-frame cadence is not enough. The useful signal must
track the real rendered frames before frame generation.

## Tested Launch Path

The RE9 Steam launch options were used to opt into the PenguinBurner Vulkan
latency layer and DXVK-NVAPI Reflex:

```text
PENGUIN_BURNER_LATENCY_SOCKET=/run/user/1000/penguin-burner/latency.sock
VK_ADD_IMPLICIT_LAYER_PATH=/home/jp/PenguinBurner/native/latency_layer/build
PENGUIN_BURNER_LATENCY_LAYER=1
PROTON_ENABLE_NVAPI=1
PROTON_HIDE_NVIDIA_GPU=0
DXVK_NVAPI_VKREFLEX=1
gamemoderun %command% /WineDetectionEnabled:False
```

Experimental flags were added and later removed during testing:

```text
PENGUIN_BURNER_LATENCY_INJECT_FRAME_IDS=1
PENGUIN_BURNER_LATENCY_GPU_TIMESTAMPS=unsafe-side-submit
PENGUIN_BURNER_LATENCY_GPU_TIMESTAMP_INTERVAL=16
```

The unsafe GPU timestamp flags are no longer present in RE9's launch options.

## VK_NV_low_latency2 Result

`VK_NV_low_latency2` was present and callable through the Proton/DXVK-NVAPI path.
The layer saw `vkSetLatencyMarkerNV` activity and could call
`vkGetLatencyTimingsNV`.

The useful report stream did not stay live.

Fresh startup samples appeared, for example:

```text
present_id=101 render_submit_us=8011 render_present_us=33065 gpu_frame_time_us=8254
present_id=221 render_submit_us=7592 render_present_us=31868 gpu_frame_time_us=8341
present_id=342 render_submit_us=8445 render_present_us=31013 gpu_frame_time_us=8338
```

After a swapchain/menu transition the driver report froze:

```text
present_id=490 driver_report_duplicate_count=2368
render_submit_us=7594 render_present_us=23992 gpu_frame_time_us=0
```

At the same time, the layer's present and marker counters continued advancing.
That means the repeated `24ms` style value was stale driver output, not a live
latency value.

The receiver now detects this state and reports:

```text
quality=stale-driver-report samples=0 missing=fresh-samples
```

## Frame ID Injection Result

The layer was modified to inject frame/present IDs using:

- `VkLatencySubmissionPresentIdNV`
- `VK_KHR_present_id`
- `VkPresentIdKHR`

Runtime verification showed the flag was present in the game process and the
rebuilt layer was loaded. It did not fix the stale report stream.

Observed behavior:

```text
present_id=1
present_id=102
present_id=222
present_id=342
present_id=406
present_id=476 repeated
```

Conclusion: missing present IDs were not the root cause for RE9. The low-latency
report stream still stopped advancing after the swapchain transition.

## GPU Timestamp Injection Result

Two GPU timestamp approaches were tried.

The original unsafe submit-wrapper approach inserted timestamp command buffers
into game submits. This class of mutation is risky and had previously caused
hard freezes.

The safer experimental mode used separate side submits:

```text
PENGUIN_BURNER_LATENCY_GPU_TIMESTAMPS=unsafe-side-submit
PENGUIN_BURNER_LATENCY_GPU_TIMESTAMP_INTERVAL=16
```

The side-submit path was able to produce `gpu-submit-proxy` samples without an
immediate hard freeze. Example samples:

```text
gpu-submit-p95=4.59ms
gpu_submit_us=880
gpu_submit_us=1135
gpu_submit_us=1109
gpu_submit_us=1290
```

But the value is only a sampled GPU submit duration. It is not the full
pre-frame-generation render latency. It also sampled small submits:

```text
gpu-submit-p95=1.29ms
gpu-submit-p95=0.15ms
gpu_submit_us=48
gpu_submit_us=74
gpu_submit_us=94
gpu_submit_us=114
```

Even after restricting sampling to queues that had presented, the metric still
did not reliably represent whole-frame render latency.

## Crash Result

The side-submit timestamp experiment eventually crashed RE9.

Evidence:

```text
coredumpctl list --since '2026-06-08 22:43:00'
Mon 2026-06-08 22:44:57 CEST 86732 1000 1000 SIGSEGV present
/home/jp/.local/share/Steam/compatibilitytools.d/Proton-CachyOS Latest/files/lib/wine/x86_64-unix/wine64-preloader
```

Core:

```text
/var/lib/systemd/coredump/core.re9\x2eexe.1000.6f2278197482486f81e62bee02ef5423.86732.1780951469000000.zst
Size on disk: 1.4G
```

The main crashing stack was in NVIDIA user-space compiler code:

```text
libnvidia-gpucomp.so.610.43.02
libnvidia-glvkspirv.so.610.43.02
libnvidia-glcore.so.610.43.02
```

No kernel `NVRM Xid` or GPU reset was found for this crash window, so this was a
game/Proton/user-space-driver crash, not a full GPU hang.

## Reflex SDK Check

The local download at `/home/jp/Downloads/Nvidia_Reflex_SDK.zip` is Reflex SDK
1.8 from 2023. Its archive contains Windows verification tools and Vulkan wrapper
artifacts:

```text
Reflex_Vulkan/inc/NvLowLatencyVk.h
Reflex_Vulkan/lib/NvLowLatencyVk.dll
Reflex_Vulkan/lib/NvLowLatencyVk.lib
Reflex_Verification/*.exe
Reflex_Verification/*.bat
```

It does not include a Linux `.so` or Linux sample binary.

Current public NVIDIA docs still list the standalone Reflex SDK requirements as
Windows 10 plus NVAPI:

```text
https://developer.nvidia.com/performance-rendering-tools/reflex
```

NVIDIA's Linux driver guide describes the Linux path differently:

```text
https://docs.nvidia.com/datacenter/tesla/driver-installation-guide/gaming.html#reflex
```

For Linux native games, Reflex is exposed directly through `VK_NV_low_latency2`,
not through the standalone Reflex SDK. For DirectX games on Proton, the supported
path is DXVK-NVAPI plus its Vulkan Reflex compatibility layer. That matches the
PenguinBurner layer approach: use the Vulkan extension and observed
DXVK-NVAPI/Proton behavior as the source of truth, not the Windows SDK DLL.

## Current Interpretation

For RE9 on this stack:

- `vkGetLatencyTimingsNV` is accessible but goes stale.
- Frame ID injection does not keep the Reflex timing stream alive.
- Driver reports can look plausible while duplicated; they must not be trusted.
- GPU timestamp side submits can produce numbers, but they are submit-level
  samples, not full render latency.
- GPU timestamp injection is not safe enough for normal gaming use because it
  crashed RE9.

## Product Decision

Do not use command-buffer timestamp injection as a PenguinBurner production
telemetry path for RE9.

> **Update (2026-06-09):** acted on this decision. The GPU-timestamp injection
> paths (`PENGUIN_BURNER_LATENCY_GPU_TIMESTAMPS=unsafe-submit-wrapper` and
> `unsafe-side-submit`, with `PENGUIN_BURNER_LATENCY_GPU_TIMESTAMP_INTERVAL`)
> and the frame-ID injection path (`PENGUIN_BURNER_LATENCY_INJECT_FRAME_IDS`)
> were **removed from the layer entirely**. They were the only sources of the
> VRAM OOMs, hard freezes, and the `libnvidia-gpucomp` SIGSEGV. The layer no
> longer intercepts `vkQueueSubmit*` at all and performs no command-buffer or
> present-info mutation. The `gpu-submit-proxy` measurement and quality level
> are gone with them. The flags above are kept here only as a historical record
> of what was tried; they are no longer recognized.
>
> Two read-only improvements landed alongside the removal:
>
> - **Per-frame emission.** `query_latency_timing` now forwards every Reflex
>   frame report whose `presentID` is newer than the last one already sent,
>   instead of only re-sending the newest report each present. This is what
>   stops the meter from latching a single stale value (the "stuck 24.3 ms").
>   When the Reflex ring stops advancing the newest report is re-emitted once so
>   the receiver's duplicate-report detection flags `quality=stale-driver-report`
>   rather than silently freezing on the old number.
> - **Real GPU render time.** The layer now emits
>   `gpu_render_us = gpuRenderEndTimeUs - gpuRenderStartTimeUs` from the Reflex
>   report, surfaced as `gpu-render-p95`. This is the per-frame
>   pre-frame-generation GPU processing time (the closest Linux equivalent to
>   the NVIDIA App overlay's render-latency number) and is the preferred adaptive
>   control signal, gated by the stale-detection above.

Keep the layer useful for diagnostics:

- log raw Reflex timing endpoints,
- detect duplicated/stale driver reports,
- expose telemetry quality explicitly,
- avoid presenting stale Reflex values as live latency.

For adaptive profile switching, RE9 still has no reliable Linux source for the
requested pre-frame-generation GPU render latency once the Reflex stream goes
stale. These signals are useful only for liveness, diagnostics, or conservative
profile decisions; they are not replacements for the missing Reflex value:

- Reflex timing only while fresh,
- present/marker diagnostics only as low-confidence data,
- NVML/GPU pressure as a conservative fallback,
- possible future external capture/perf tooling, but not in-process submit
  mutation.

## RE9 Recovery and DXVK-NVAPI Injection Retries, 2026-06-09

Several live retries were run with the rebuilt read-only layer and RE9 Steam app
`3764200`. The normal Steam launch options were restored afterwards to:

```text
PENGUIN_BURNER_LATENCY_SOCKET=/run/user/1000/penguin-burner/latency.sock
VK_ADD_IMPLICIT_LAYER_PATH=/home/jp/PenguinBurner/native/latency_layer/build
PENGUIN_BURNER_LATENCY_LAYER=1
PROTON_ENABLE_NVAPI=1
PROTON_HIDE_NVIDIA_GPU=0
DXVK_NVAPI_VKREFLEX=1
gamemoderun %command% /WineDetectionEnabled:False
```

The first retry confirmed that the swapchain latency create-info was present
(`swapchain_latency_mode=True`) and that replaying the captured
`vkSetLatencySleepModeNV` state after swapchain creation succeeds
(`result=0`). Once the Reflex timing ring stalled, the layer repeatedly replayed
the saved sleep-mode state and Vulkan accepted those calls, but
`vkGetLatencyTimingsNV` kept returning the same stale `present_id` instead of
resuming. This run froze at `present_id=469`; duplicate count reached `8918`.

The second retry tested the stronger reset probe that called
`vkSetLatencySleepModeNV(device, swapchain, nullptr)` at the stale threshold.
The log stopped at `present_id=462` with
`driver_report_duplicate_count=240`, and Steam removed the RE9 process at
`20:06:01`. No reset-result event was emitted, which means the process exited
before the reset call returned. That reset path is therefore opt-in only via
`PENGUIN_BURNER_LATENCY_RECOVERY_RESET=1`.

The default recovery was then changed to a safer off/on toggle:

1. call `vkSetLatencySleepModeNV` with the saved mode but
   `lowLatencyMode=false`, `lowLatencyBoost=false`;
2. replay the game's last saved sleep-mode state.

RE9 stayed alive, and both calls returned `result=0`, but the driver timing ring
still did not recover. The run froze at `present_id=473` and ended with
`quality=stale-driver-report ... gpu-render-p95=n/a`.

DXVK-NVAPI's own Vulkan Reflex layer also has frame-ID injection knobs:

```text
DXVK_NVAPI_VKREFLEX_INJECT_PRESENT_FRAME_IDS=1
DXVK_NVAPI_VKREFLEX_INJECT_SUBMIT_FRAME_IDS=1
```

Both were tested because `third_party/dxvk-nvapi/layer/vulkan_reflex_layer.cpp`
defaults them to disabled and its README describes them as correlation helpers.
They did not make RE9 reliable:

- Present-only injection froze at `present_id=496`; duplicate count reached
  `4581`.
- Submit+present injection froze at `present_id=487`; duplicate count reached
  `6042`.

The submit+present run briefly produced fresh reports after a later swapchain
creation, but only for about 59 reports (`present_id=359` through `418`). It
then collapsed immediately to IDs `483..487` repeating. That short pulse is not
a sustained recovery.

Conclusion: layer-level sleep-mode replay, sleep-mode off/on recovery, and
DXVK-NVAPI submit/present frame-ID injection do not recover the true
`gpu_render_us` stream in RE9. The correct behavior for PenguinBurner is to mark
the Reflex value stale and avoid showing a frozen latency number.

## Source Inspection

Local source checkouts used:

- `third_party/vkd3d-proton` (`HansKristian-Work/vkd3d-proton`, HEAD
  `210e774` during inspection)
- `third_party/dxvk-nvapi` (`jp7677/dxvk-nvapi`, HEAD `46c300b`, with local
  telemetry edits in `layer/vulkan_reflex_layer.cpp`)
- scratch clones `third_party/proton`, `third_party/dxvk`, and dated
  `third_party/proton-cachyos-*` branch snapshots

Important version correction: the installed `files/lib/vkd3d/version` entry is
Wine VKD3D, not the active D3D12 translation path. RE9's D3D12 path uses
`files/lib/wine/vkd3d-proton/version`.

Installed VKD3D-Proton versions checked:

- `Proton-CachyOS Latest` / `cachyos-11.0-20260506-slr`:
  `64f5776f` (`v3.0.1-2-g64f5776f`)
- `proton-cachyos-11.0-20260520-slr-x86_64`:
  `84a46a23` (`v3.0.1-93-g84a46a23`)
- `proton-cachyos-11.0-20260521-slr-x86_64` and Steam Proton Experimental:
  `110e8bd4` (`v3.0.1-143-g110e8bd4`)
- Steam Proton Hotfix: `6062cc70` (`v3.0.1-154-g6062cc70`)

The current RE9 baseline (`64f5776f`) already contains upstream commits
`298eaf00` (multi-swapchain Reflex guard), `c8c8ab50` (DLSS Frame Generation
Reflex desync/freeze fix), and `e72c0753` (out-of-band present marker mapping
for downstream latency tools). So this is not simply a missing upstream Reflex
mapping fix in the installed Proton.

The relevant D3D12 path is VKD3D-Proton's `ID3DLowLatencyDevice`
implementation, not generic Proton launcher code. VKD3D stores one
`low_latency_swapchain` per D3D12 device, clears it when more than one Vulkan
swapchain exists, resets present-ID state during swapchain destruction, and only
attaches `VkLatencySubmissionPresentIdNV` when a low-latency swapchain exists
and low-latency mode is enabled. DXVK-NVAPI's Vulkan layer also remaps fake
NVAPI swapchain handles to a single latest real Vulkan swapchain.

That makes the next debugging target handle-level flow, not another sleep-mode
toggle: log whether `vkLatencySleepNV`, `vkQueueNotifyOutOfBandNV`, Reflex
marker IDs, Vulkan present IDs, live Vulkan swapchain count, and driver timing
report IDs keep advancing across the RE9 menu-to-gameplay transition.
`PENGUIN_BURNER_LATENCY_DEBUG_FLOW=1` enables extra `present-flow` snapshots;
stale threshold snapshots are emitted as `status=latency-stream-stale` even
without the verbose flag. If `live_swapchain_count` rises above 1 near the stale
point, VKD3D-Proton's intentional multi-swapchain Reflex disable path is the
leading suspect; if it stays 1 while markers advance beyond
`last_driver_report_present_id`, the NVIDIA timing ring is stalling despite a
valid marker feed.

## No-build Present-mode Probe

VKD3D-Proton accepts `VKD3D_SWAPCHAIN_PRESENT_MODE` with uppercase Vulkan mode
names: `IMMEDIATE`, `MAILBOX`, `FIFO`, `FIFO_RELAXED`, or `FIFO_LATEST_READY`.
In source, a supported override sets `present.override_present_mode=true`.
That specifically prevents swapchain recreation caused only by the boolean
`swap_interval` transition; it does **not** prevent recreation for color-space
or DXGI format changes.

The RE9 launch line used for the next test is:

```text
PENGUIN_BURNER_LATENCY_SOCKET=/run/user/1000/penguin-burner/latency.sock VK_ADD_IMPLICIT_LAYER_PATH=/home/jp/PenguinBurner/native/latency_layer/build PENGUIN_BURNER_LATENCY_LAYER=1 PENGUIN_BURNER_LATENCY_DEBUG_FLOW=1 VKD3D_SWAPCHAIN_PRESENT_MODE=IMMEDIATE PROTON_ENABLE_NVAPI=1 PROTON_HIDE_NVIDIA_GPU=0 DXVK_NVAPI_VKREFLEX=1 gamemoderun %command% /WineDetectionEnabled:False
```

Read the result as follows:

- `create-swapchain ... present_mode_name=IMMEDIATE` confirms the override
  reached VKD3D and the driver accepted it.
- If `gpu_render_us` remains fresh through the menu-to-gameplay transition, the
  likely trigger was VKD3D's swap-interval recreation path and the no-build
  workaround is to keep `VKD3D_SWAPCHAIN_PRESENT_MODE=IMMEDIATE` for RE9.
- If `latency-stream-stale` still appears with `live_swapchain_count > 1`, the
  remaining suspect is VKD3D-Proton clearing `low_latency_swapchain` when more
  than one Vulkan swapchain exists.
- If `latency-stream-stale` appears with `live_swapchain_count=1` while marker
  IDs and Vulkan present IDs advance beyond `last_driver_report_present_id`, the
  app/VKD3D side is feeding frames and the NVIDIA timing report ring itself is
  stale.

## Custom VKD3D-Proton Fallback

If the `IMMEDIATE` no-build probe still stalls and the analyzer reports
`root_cause=vkd3d-multi-swapchain-reflex-guard`, the next experiment is a custom
VKD3D-Proton build, not another PenguinBurner layer toggle.

Patch candidate:

```text
docs/patches/vkd3d-proton-re9-allow-multi-swapchain-reflex.patch
```

The patch is intentionally opt-in. It adds
`VKD3D_LOW_LATENCY_ALLOW_MULTI_SWAPCHAIN=1`, which keeps VKD3D-Proton's current
`low_latency_swapchain` owner instead of clearing it when
`vk_swapchain_count > 1`. This is a diagnostic patch, not a general upstream
proposal. A successful run would mean the stall is caused by VKD3D-Proton's
multi-swapchain Reflex ownership guard. A failed run, especially with
`live_swapchain_count=1` or markers/presents advancing beyond the last driver
report, points back to the NVIDIA timing report ring.

Installed Proton-CachyOS stores VKD3D-Proton here:

```text
files/lib/wine/vkd3d-proton/x86_64-windows/d3d12.dll
files/lib/wine/vkd3d-proton/x86_64-windows/d3d12core.dll
```

Build status on this host:

- `third_party/vkd3d-proton` was fetched from the public upstream HTTPS remote
  and submodules were initialized without credentials.
- The patch applies cleanly to upstream `vkd3d-proton` HEAD `210e7741`.
- The host only has `ninja` locally, so the 64-bit DLLs were built inside the
  public Proton SDK container
  `registry.gitlab.steamos.cloud/proton/steamrt4/sdk/x86_64:4.0.20260331.220802-0`.
- Built artifacts:
  `third_party/vkd3d-proton-re9-build/prefix/x64/d3d12.dll` and
  `third_party/vkd3d-proton-re9-build/prefix/x64/d3d12core.dll`.
- `strings d3d12core.dll` confirms the patched
  `VKD3D_LOW_LATENCY_ALLOW_MULTI_SWAPCHAIN` path is present.

A safe Steam test copy now exists at:

```text
/home/jp/.local/share/Steam/compatibilitytools.d/Proton-CachyOS PB-Re9-Reflex
```

Only the copied tool's
`files/lib/wine/vkd3d-proton/x86_64-windows/d3d12.dll` and
`d3d12core.dll` were replaced. The original `Proton-CachyOS Latest` payload was
not overwritten.

For the patched proof run, RE9 must use the copied compatibility tool and the
launch line must add `VKD3D_LOW_LATENCY_ALLOW_MULTI_SWAPCHAIN=1`:

```text
PENGUIN_BURNER_LATENCY_SOCKET=/run/user/1000/penguin-burner/latency.sock VK_ADD_IMPLICIT_LAYER_PATH=/home/jp/PenguinBurner/native/latency_layer/build PENGUIN_BURNER_LATENCY_LAYER=1 PENGUIN_BURNER_LATENCY_DEBUG_FLOW=1 VKD3D_SWAPCHAIN_PRESENT_MODE=IMMEDIATE VKD3D_LOW_LATENCY_ALLOW_MULTI_SWAPCHAIN=1 PROTON_ENABLE_NVAPI=1 PROTON_HIDE_NVIDIA_GPU=0 DXVK_NVAPI_VKREFLEX=1 gamemoderun %command% /WineDetectionEnabled:False
```

The guarded setup command applies both pieces after Steam is closed:

```bash
penguin-burner-steam-re9-patched-setup --wait
penguin-burner-steam-launch-check \
  --extra-require VKD3D_LOW_LATENCY_ALLOW_MULTI_SWAPCHAIN=1 \
  --compat-tool 'Proton-CachyOS PB-Re9-Reflex'
```

Without `--wait`, it refuses to write while Steam or a Wine game process is
still running. With `--wait`, it waits until those processes exit and writes
`.pburn-bak` backups next to the edited Steam config files.

If this run keeps fresh `gpu_render_us` through menu-to-gameplay while the
previous run reported `root_cause=vkd3d-multi-swapchain-reflex-guard`, the
actual workaround is a VKD3D-Proton build that keeps the low-latency swapchain
owner across RE9's second live swapchain. If it still stalls, the remaining
cause is lower than VKD3D ownership: the NVIDIA Reflex timing report stream
itself is stale or the driver cannot correlate the submitted/presented frame IDs.

### Patched VKD3D-Proton Test Result

Live RE9 test on 2026-06-09 used:

- compatibility tool: `Proton-CachyOS PB-Re9-Reflex`
- launch option: `VKD3D_LOW_LATENCY_ALLOW_MULTI_SWAPCHAIN=1`
- launch option: `VKD3D_SWAPCHAIN_PRESENT_MODE=IMMEDIATE`
- launch option: `PENGUIN_BURNER_LATENCY_DEBUG_FLOW=1`

Preflight confirmed the copied compatibility tool and patched
`d3d12core.dll` were active. The layer and DXVK-NVAPI Reflex path were also
active.

The patched VKD3D run did **not** prove the multi-swapchain guard as the active
RE9 failure. In the stale interval the highest live swapchain count stayed at
`1`, so there was no second live swapchain for the diagnostic patch to save.

Folded capture:

```text
~/.cache/penguin-burner/latency-captures/re9-live-20260609-214430-214710-folded.log
```

Analyzer result:

```text
root_cause=driver-report-stale-after-reflex-markers
stale_events=4
highest_live_swapchain_count=1
distinct_raw_present_ids=108
distinct_gpu_render_us=0
raw_driver_timestamp_samples=2
latest_stale.last_driver_report_present_id=8298
latest_stale.last_vulkan_present_id=0
latest_stale.max_marker_present_id=8544
latest_stale.driver_report_duplicate_count=240
latest_stale.swapchain_latency_mode=True
```

The two raw driver timestamp samples were duplicate reports for an old
`present_id` and still had `gpu_render_start_us=0` and
`gpu_render_end_us=0`, so they did not provide a real GPU render duration.
Meter output briefly showed fresh-looking values before the transition, then
dropped to:

```text
gpu-render-p95=n/a missing=input-sample,driver-timing
```

Conclusion: the custom VKD3D-Proton multi-swapchain patch is not a usable RE9
workaround on this run. The active failure is the NVIDIA
`VK_NV_low_latency2` timing report stream becoming stale/partial while Reflex
markers keep advancing. PenguinBurner should keep marking this state stale
instead of showing a frozen render-latency number.

## Debugging

For a step-by-step guide to building the layer, capturing telemetry, reading the
log fields, and verifying the per-frame emission and `gpu_render_us` fixes, see
[Latency Telemetry Debugging Guide](./latency-telemetry-debugging.md).

## Useful Commands

Check raw timing:

```bash
journalctl -u PenguinBurner.service --since '2026-06-08 22:43:00' -o cat \
  | rg -A3 'event=latency-raw'
```

Check meter summaries:

```bash
journalctl -u PenguinBurner.service --since '2026-06-08 22:43:00' -o cat \
  | rg -A2 'event=latency-meter'
```

Check marker coverage:

```bash
journalctl -u PenguinBurner.service --since '2026-06-08 22:43:00' -o cat \
  | rg -A2 'status=latency-marker-coverage'
```

Check coredump:

```bash
coredumpctl info 86732 --no-pager
```

Check for kernel NVIDIA faults:

```bash
journalctl -k --since '2026-06-08 22:43:00' -o short-iso \
  | rg -i 'NVRM|Xid|nvidia|gpu|segfault|trap|re9|oom|killed'
```
