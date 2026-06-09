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

For adaptive profile switching, RE9 should fall back to safer signals:

- Reflex timing only while fresh,
- present/marker diagnostics only as low-confidence data,
- NVML/GPU pressure as a conservative fallback,
- possible future external capture/perf tooling, but not in-process submit
  mutation.

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
