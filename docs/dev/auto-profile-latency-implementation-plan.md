# Auto Profile Latency Telemetry Implementation Plan

Date: 2026-06-03

## Goal

Add an opt-in game-process telemetry path that lets the PenguinBurner runtime
choose saved GPU profiles from real frame/latency pressure instead of visible
FPS alone.

Durable constraints:

- The PenguinBurner daemon cannot call `VK_NV_low_latency2` for another process.
  `vkGetLatencyTimingsNV` requires the game's `VkDevice` and `VkSwapchainKHR`.
- The Linux/Proton Reflex path is already translated by DXVK, VKD3D-Proton, and
  DXVK-NVAPI. PenguinBurner should not reimplement NVAPI or DirectX translation.
- Injection must be opt-in per launch. Do not install an always-active global
  hook.

## What The Launch Env Vars Do

Example launch:

```bash
PENGUIN_BURNER_LATENCY_LAYER=1 DXVK_NVAPI_VKREFLEX=1 %command%
```

`PENGUIN_BURNER_LATENCY_LAYER=1` should be PenguinBurner's future enable gate.
It should only activate PenguinBurner's telemetry Vulkan implicit layer when its
manifest has been discovered by the Vulkan loader. Until PenguinBurner ships that
layer and manifest, this variable does nothing by itself.

`DXVK_NVAPI_VKREFLEX=1` is an existing DXVK-NVAPI enable gate. DXVK-NVAPI's
Vulkan Reflex layer is installed as an implicit Vulkan layer, but its manifest
keeps it disabled unless this variable is set. When enabled, the layer:

- exposes/forwards the Vulkan Reflex compatibility path used by DXVK-NVAPI;
- adds `VK_NV_low_latency2` to device creation when the driver supports it;
- captures the real `VkSwapchainKHR`;
- patches DXVK-NVAPI's fake swapchain handle calls into calls against the real
  swapchain;
- forwards `vkSetLatencySleepModeNV`, `vkLatencySleepNV`,
  `vkSetLatencyMarkerNV`, `vkGetLatencyTimingsNV`, and
  `vkQueueNotifyOutOfBandNV`.

Important: if DXVK-NVAPI's layer is not installed/enabled, some game paths can
receive fake success from DXVK-NVAPI so the title does not break, but real Reflex
latency reduction is not active and PenguinBurner should not treat that as
high-quality telemetry.

## Recommended Architecture

Use a PenguinBurner Vulkan implicit layer as the telemetry producer and keep the
existing Python daemon/runtime loop as the policy controller.

```text
Game / Proton process
  -> DXVK or VKD3D-Proton
  -> DXVK-NVAPI Reflex path where applicable
  -> PenguinBurner Vulkan implicit telemetry layer
  -> NVIDIA driver VK_NV_low_latency2 timing query
  -> Unix datagram socket or shared memory ring

PenguinBurner runtime daemon
  -> reads telemetry samples
  -> classifies telemetry quality
  -> applies saved Auto-UV profiles
```

Start with a sibling telemetry layer rather than patching DXVK-NVAPI:

- lower maintenance risk for PenguinBurner packaging;
- independent release cadence;
- no need to carry a downstream DXVK-NVAPI fork;
- can also observe native Vulkan titles that use `VK_NV_low_latency2`.

If a sibling layer cannot reliably run after the Reflex compatibility layer or
cannot see valid timings, the fallback is a small downstream patch to
DXVK-NVAPI's existing layer because it already captures the exact swapchain used
to translate fake handles.

## Layer Responsibilities

The PenguinBurner layer should:

1. Be an implicit Vulkan layer with an `enable_environment` gate:
   `PENGUIN_BURNER_LATENCY_LAYER=1`.
2. Capture `VkInstance`, `VkPhysicalDevice`, `VkDevice`, queues, and swapchains.
3. Ensure `VK_NV_low_latency2` is enabled only when:
   - the physical device advertises it;
   - the app did not already enable it;
   - the layer can safely add required dependencies/features.
4. Track active swapchains from `vkCreateSwapchainKHR` and
   `vkDestroySwapchainKHR`.
5. Observe markers by wrapping `vkSetLatencyMarkerNV`.
6. Query `vkGetLatencyTimingsNV` after `vkQueuePresentKHR`, throttled to avoid
   overhead.
7. Export compact samples to the daemon:
   - process id, executable/application name if available;
   - device PCI identity when possible;
   - swapchain id;
   - present id;
   - marker/timing fields;
   - derived render/app/gpu frame times;
   - quality enum;
   - monotonic sample time.

Do not enable `lowLatencyBoost` or change pacing policy. PenguinBurner only needs
telemetry; the game and driver own the actual Reflex behavior.

## Telemetry Quality

Classify each sample explicitly:

- `reflex-markers`: valid app-provided marker timings are present.
- `driver-timing`: `VK_NV_low_latency2` reports driver/GPU/present timings but
  marker coverage is incomplete.
- `present-frametime`: only present cadence can be derived.
- `gamescope`: compositor-side fallback, outside this layer.
- `gpu-pressure`: NVML fallback, outside this layer.
- `none`: no usable signal.

Initial profile switching should use p95 frame/latency pressure over a rolling
window, with promotion faster than demotion and a cooldown to avoid flapping.

## Options

### Option A: Sibling PenguinBurner Layer

Upside: clean ownership, packageable with PenguinBurner, works for native Vulkan
and Proton if ordering is compatible.

Downside: must implement correct Vulkan layer dispatch and extension injection.
Layer ordering with DXVK-NVAPI must be tested.

Cost: medium.

Risk: medium. Vulkan layer mistakes can break game startup.

### Option B: Patch DXVK-NVAPI Reflex Layer

Upside: easiest access to the real swapchain and translated Reflex calls for
Proton DX12/D3D11 paths that need DXVK-NVAPI.

Downside: creates a downstream maintenance burden or requires upstreaming a
PenguinBurner-specific telemetry export.

Cost: low for prototype, high for maintenance.

Risk: medium-high due to dependency on DXVK-NVAPI internals and releases.

### Option C: No Injection Fallbacks Only

Upside: safest operationally; no process hook.

Downside: cannot access official Reflex timing reports. Cannot reliably
distinguish real rendered cadence from frame generation or compositor behavior.

Cost: low.

Risk: low technically, high product risk because signal quality is weak.

## Prototype Plan

Current scaffold status:

- Native layer source lives under `native/latency_layer`.
- The build produces `libVkLayer_penguinburner_latency.so` and
  `VkLayer_PENGUINBURNER_latency.json`.
- The layer is opt-in via `PENGUIN_BURNER_LATENCY_LAYER=1`.
- It currently observes Vulkan object creation, tracks queues/swapchains,
  records `vkSetLatencyMarkerNV`, queries `vkGetLatencyTimingsNV` after
  `vkQueuePresentKHR`, and sends nonblocking JSON datagrams to
  `$XDG_RUNTIME_DIR/penguin-burner/latency.sock` or
  `PENGUIN_BURNER_LATENCY_SOCKET`.
- Normal runtime now binds that socket and logs `event=latency-meter` summaries
  to stdout/systemd journal every 10 seconds once samples arrive.
- It does not yet force-enable `VK_NV_low_latency2`, feed samples into the
  profile policy, or switch profiles.

Build and check the current scaffold:

```bash
cmake -S native/latency_layer -B /tmp/penguinburner-latency-layer-build
cmake --build /tmp/penguinburner-latency-layer-build

VK_ADD_IMPLICIT_LAYER_PATH=/tmp/penguinburner-latency-layer-build \
python penguin_burner.py --check-latency-layer
```

Build-tree Steam launch test:

```bash
VK_ADD_IMPLICIT_LAYER_PATH=/tmp/penguinburner-latency-layer-build \
PENGUIN_BURNER_LATENCY_LAYER=1 \
DXVK_NVAPI_VKREFLEX=1 \
%command%
```

Expected runtime journal line after samples arrive:

```text
event=latency-meter quality=reflex-markers samples=37 age=0.2s pid=1234 present-id=9001 gpu-frame-p95=16.67ms input-present-p95=33.00ms render-submit-p95=2.10ms
```

1. Build a minimal C/C++ implicit layer:
   - manifest: `VK_LAYER_PENGUINBURNER_latency`;
   - library: `libVkLayer_penguinburner_latency.so`;
   - enable env: `PENGUIN_BURNER_LATENCY_LAYER=1`;
   - disable env: `DISABLE_PENGUIN_BURNER_LATENCY_LAYER`.
2. Verify discovery:
   ```bash
   VK_ADD_IMPLICIT_LAYER_PATH=/path/to/layer \
   PENGUIN_BURNER_LATENCY_LAYER=1 \
   vulkaninfo --summary
   ```
3. Wrap `vkCreateInstance`, `vkCreateDevice`, `vkCreateSwapchainKHR`,
   `vkDestroySwapchainKHR`, `vkQueuePresentKHR`, `vkSetLatencyMarkerNV`, and
   `vkGetLatencyTimingsNV`.
4. First telemetry proof:
   - log to stderr or a temp file only in debug builds;
   - capture timing count and nonzero `presentID`;
   - compute `gpuRenderEndTimeUs - previous.gpuRenderEndTimeUs`.
5. Replace debug logging with a Unix datagram socket:
   - path under `$XDG_RUNTIME_DIR/penguin-burner/latency.sock`;
   - nonblocking send;
   - drop samples on backpressure.
6. Add daemon receiver:
   - parse fixed binary or newline JSON samples;
   - expose runtime status text;
   - keep policy disabled until telemetry quality and target profile are
     selected explicitly.
7. Test Proton launch:
   ```bash
   VK_ADD_IMPLICIT_LAYER_PATH=/path/to/penguinburner/layer:/path/to/dxvk-nvapi/layer \
   PENGUIN_BURNER_LATENCY_LAYER=1 \
   DXVK_NVAPI_VKREFLEX=1 \
   DXVK_NVAPI_VKREFLEX_LAYER_LOG_LEVEL=info \
   %command%
   ```
8. Validate against:
   - a native Vulkan app with `VK_NV_low_latency2` if available;
   - a DXVK D3D11 Reflex title;
   - a VKD3D-Proton D3D12 Reflex title;
   - a game with frame generation enabled.

## Research Notes

Repos inspected under `/tmp/penguinburner-research`:

- `jp7677/dxvk-nvapi` at `8f5f345`
- `HansKristian-Work/vkd3d-proton` at `6062cc7`
- `doitsujin/dxvk` at `95db0b2`
- `KhronosGroup/Vulkan-Loader` at `9fe2d47`
- `Korthos-Software/low_latency_layer` at `4e7fe12`

Key findings:

- DXVK-NVAPI documents that Vulkan Reflex requires an additional implicit layer,
  enabled by `DXVK_NVAPI_VKREFLEX=1`.
- DXVK-NVAPI's layer adds `VK_NV_low_latency2`, enables timeline semaphore, and
  captures the real swapchain at `vkCreateSwapchainKHR`.
- DXVK-NVAPI passes fake swapchain handles through Wine, then its Linux-side
  layer swaps those calls to the captured real swapchain.
- VKD3D-Proton's D3D12 low-latency path directly calls
  `vkGetLatencyTimingsNV` and maps the Vulkan timing report into D3D12 latency
  results.
- DXVK's D3D11 path uses `DxvkReflexLatencyTrackerNv` and also queries
  `vkGetLatencyTimingsNV`, with additional timestamp correction.
- The community `low_latency_layer` is useful as layer scaffolding, but when it
  emulates Reflex it returns zero timing count for `vkGetLatencyTimingsNV`, so it
  is not a source for official NVIDIA telemetry.

## Source Links

- NVIDIA Linux Reflex notes:
  <https://docs.nvidia.com/datacenter/tesla/driver-installation-guide/gaming.html#reflex>
- Vulkan `VK_NV_low_latency2`:
  <https://docs.vulkan.org/refpages/latest/refpages/source/VK_NV_low_latency2.html>
- Vulkan `vkGetLatencyTimingsNV`:
  <https://docs.vulkan.org/refpages/latest/refpages/source/vkGetLatencyTimingsNV.html>
- Vulkan timing report structure:
  <https://registry.khronos.org/vulkan/specs/latest/man/html/VkLatencyTimingsFrameReportNV.html>
- Vulkan implicit layer interface:
  <https://github.com/KhronosGroup/Vulkan-Loader/blob/main/docs/LoaderLayerInterface.md>
