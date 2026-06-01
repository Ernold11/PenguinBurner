# Auto Profile Latency Telemetry Findings

Date: 2026-06-01

## Goal

PenguinBurner should be able to switch saved GPU profiles automatically:

- Default to an efficient profile when real render pressure is low.
- Promote to balanced/performance when the game misses a target such as 60 FPS.
- Avoid being fooled by NVIDIA frame generation or other presentation tricks.

The control signal should therefore prefer raw render/app frametime or Reflex
latency timing over visible FPS.

## Current Runtime Fit

PenguinBurner already has a daemon/runtime loop that applies one selected V/F
curve, monitors GPU telemetry, and re-applies curve state after driver resets.
That daemon is the right controller for adaptive profile switching. A separate
service is not needed.

The missing piece is an FPS/latency telemetry source. The daemon cannot derive
true game frametime from NVML alone.

## MangoHud Findings

Plain MangoHud can measure FPS and frametime, but the currently documented clean
external paths are not suitable as the primary PenguinBurner source:

- MangoHud FPS logging writes files. We do not want logfile tailing.
- MangoHud `control=` creates a Unix socket, but the current control protocol is
  command oriented. It handles actions such as toggling HUD/logging and does not
  provide a live FPS/frametime telemetry stream.
- MangoHud's `mangoapp` mode can show gamescope app frametime and latency debug
  data, but that data originates from gamescope.

Conclusion: plain MangoHud would need an upstream or local stats-export patch,
for example a `subscribe-stats` command over its control socket. Without that,
MangoHud is not the clean no-file telemetry source.

## Gamescope Findings

Gamescope can provide useful frametime data without patching MangoHud:

- Its `mangoapp` IPC message includes app frametime, visible frametime, latency,
  PID, output size, refresh, HDR focus fields, and engine name.
- Its private `gamescope_control` Wayland protocol has
  `request_app_performance_stats` and `app_performance_stats` frametime events.

This means a gamescope source does not require MangoHud to be visible or used as
the control signal. MangoHud/mangoapp is only an existing consumer of the data.

Tradeoff: gamescope data is still compositor/presentation-side data. It is useful
and no-patch, but it cannot universally prove whether NVIDIA frame generation is
active or identify the real input-bearing rendered frame cadence in every title.

## NVIDIA Reflex / VKD3D-Proton Findings

For DX12 Proton games, the most relevant stack is:

```text
D3D12 game
  -> DXVK-NVAPI handles NVIDIA NVAPI / Reflex calls
  -> VKD3D-Proton translates D3D12 to Vulkan
  -> NVIDIA Vulkan driver exposes VK_NV_low_latency2
```

The official Linux-facing Reflex-style API is `VK_NV_low_latency2`, especially:

- `vkSetLatencyMarkerNV`
- `vkGetLatencyTimingsNV`
- `vkSetLatencySleepModeNV`
- `vkLatencySleepNV`

`vkGetLatencyTimingsNV` can return per-frame timing reports with fields for
input sample, simulation, render submit, present, driver, OS render queue, and
GPU render timing when the game/translation stack provides the relevant markers.

Important constraint: this API requires the game's `VkDevice` and `VkSwapchainKHR`.
The PenguinBurner daemon cannot call it externally for another process. Any
official implementation must run inside the game process.

## Recommended Architecture

Use the PenguinBurner daemon as the policy controller, plus a small in-process
telemetry component for official latency timing:

```text
DX12 game under Proton
  -> DXVK-NVAPI / VKD3D-Proton
  -> PenguinBurner Vulkan implicit layer
  -> VK_NV_low_latency2 timing query
  -> Unix socket or shared memory telemetry

PenguinBurner daemon
  -> reads telemetry
  -> applies efficiency/balanced/performance profiles
```

The Vulkan layer should be opt-in per launch, not installed as a globally active
always-on hook:

```bash
PENGUIN_BURNER_LATENCY_LAYER=1 DXVK_NVAPI_VKREFLEX=1 %command%
```

This avoids injecting into launchers, unrelated Vulkan apps, or anti-cheat
titles by default.

## Telemetry Quality Levels

The daemon should record telemetry quality explicitly:

```text
quality=reflex-markers     true Reflex/low-latency markers available
quality=driver-timing      driver/GPU/present timing available, markers partial
quality=present-frametime  frame pacing only
quality=gamescope          compositor frametime source
quality=gpu-pressure       NVML heuristic fallback
quality=none               no usable source
```

Profile switching should prefer the highest-quality available source. If only
visible FPS or present frametime is available, the daemon should not claim it can
detect frame generation reliably.

## Switching Signal

Use frametime rather than visible FPS:

```text
target_frametime_ms = 1000 / target_fps
60 FPS target = 16.67 ms
```

Initial policy:

- Promote when p95 raw/render/app frametime exceeds target for a sustained window.
- Demote only after a longer stable recovery window.
- Use cooldowns to avoid profile flapping.
- Log both the chosen telemetry source and quality.

If frame generation is present, visible FPS may look healthy while raw render
frametime or latency remains poor. In that case PenguinBurner should boost based
on the raw/latency-side signal, not the generated visible cadence.

## Open Risks

- Not every DX12 Proton game uses Reflex/NVAPI latency markers.
- Without markers, timing may degrade to present/render-adjacent data.
- Anti-cheat titles may reject Vulkan layers or NVAPI wrappers.
- `gamescope_control` is a private protocol and may change.
- Reverse engineered driver calls should remain experimental and disabled by
  default unless the official path proves insufficient.

## Source Links

- Vulkan `VK_NV_low_latency2`:
  <https://docs.vulkan.org/refpages/latest/refpages/source/VK_NV_low_latency2.html>
- Vulkan `vkGetLatencyTimingsNV`:
  <https://docs.vulkan.org/refpages/latest/refpages/source/vkGetLatencyTimingsNV.html>
- Vulkan latency timing report:
  <https://docs.vulkan.org/refpages/latest/refpages/source/VkLatencyTimingsFrameReportNV.html>
- NVIDIA Linux gaming / Reflex notes:
  <https://docs.nvidia.com/datacenter/tesla/driver-installation-guide/gaming.html#reflex>
- DXVK-NVAPI:
  <https://github.com/jp7677/dxvk-nvapi>
- VKD3D-Proton:
  <https://github.com/HansKristian-Work/vkd3d-proton>
- MangoHud:
  <https://github.com/flightlessmango/MangoHud>
- Gamescope mangoapp data path:
  <https://github.com/ValveSoftware/gamescope/blob/master/src/mangoapp.cpp>
- Gamescope private control protocol:
  <https://github.com/ValveSoftware/gamescope/blob/master/protocol/gamescope-control.xml>
