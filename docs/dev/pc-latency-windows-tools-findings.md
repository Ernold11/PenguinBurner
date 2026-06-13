# PC Latency: How Windows Tools Measure It, and the Linux Equivalent

Date: 2026-06-11

Goal: show a real Reflex latency number in milliseconds in the PB overlay,
next to the pre-frame-generation FPS, computed the same way NVIDIA's own
tooling computes it — or as close as the Proton stack allows.

Related docs:

- [latency-meter-implementation-plan.md](./latency-meter-implementation-plan.md)
  (the implementation this research validated; Phases 1–2 plus the overlay
  slice of Phase 4 are implemented as of 2026-06-11)
- [re9-latency-experiment-findings.md](./re9-latency-experiment-findings.md)
- [superpowers/specs/2026-06-10-latency-meter-design.md](../superpowers/specs/2026-06-10-latency-meter-design.md)

## How NVIDIA App / FrameView measure "PC Latency" (PCL)

Sources: NVIDIA's [Understanding and Measuring PC Latency](https://developer.nvidia.com/blog/understanding-and-measuring-pc-latency/),
the [Streamline PCL guide](https://github.com/NVIDIAGameWorks/Streamline/blob/main/docs/ProgrammingGuidePCL.md),
and the local FrameView SDK install
(`/home/jp/win/Program Files/NVIDIA Corporation/FrameViewSDK/README.txt`):

> `MsPCLatency` — Time between PC receiving an input and frame being sent to
> the display, in milliseconds. Supported in Reflex 1.6 titles / titles with
> PC Latency Stats ("NA" if unsupported or in menus)

Decomposition:

```text
PCL = I2FS + FS2P + P2D
```

- **I2FS (input → frame start)**: time from the input event to the next
  frame's simulation start. Averages roughly the input sampling interval.
- **FS2P (frame start → present)**: simulation start through completion of
  the Present() call. The CPU/render-pipeline component — the largest one.
- **P2D (present → displayed)**: Present() call to the actual framebuffer
  flip on the display.

The key insight: **PCL is marker-based, not measured end-to-end.** The game
integrates PC Latency Stats (part of the Reflex SDK):

1. The game posts itself synthetic "ping" messages at random 100–300 ms
   intervals and emits a `PCLStatsInput` ETW event for each.
2. The frame whose simulation picks up a ping is tagged with a
   `PC_LATENCY_PING` latency marker.
3. FrameView / NVIDIA App stitch ping-time → `SIMULATION_START` →
   `PRESENT_END` → flip (from DXGI ETW) into the reported PCL. Frames
   between pings are interpolated.

## How Intel PresentMon measures input latency

Sources: [PresentMon console docs](https://raw.githubusercontent.com/GameTechDev/PresentMon/main/README-ConsoleApplication.md),
[issue #366](https://github.com/GameTechDev/PresentMon/issues/366).

Two metric families:

- **Click-to-photon / all-input-to-photon**: correlates real OS input events
  (win32k ETW providers) with the displayed frame. Requires Windows input
  tracing — no Proton equivalent exists.
- **Instrumented latency**: the same marker approach as NVIDIA's PCL Stats
  (supports PCLStats and Intel XeLL markers): frame start to display.

## What survives the Proton/dxvk-nvapi path (verified locally)

- dxvk-nvapi translates `NvAPI_D3D_SetLatencyMarker` to
  `VK_NV_low_latency2` `vkSetLatencyMarkerNV`, which the PenguinBurner layer
  already hooks. But **`PC_LATENCY_PING` is silently dropped** —
  `third_party/dxvk-nvapi/src/nvapi/nvapi_d3d_low_latency_device.cpp:91`
  returns nothing for it because `VkLatencyMarkerNV` has no equivalent
  value. Even if it were forwarded, nothing on Linux posts the pings (the
  pings are normally triggered by FrameView/NVIDIA App via window messages).
- **RE9 emits zero `INPUT_SAMPLE` markers**: `last_input_sample_present_id=0`
  across all 11,861 snapshots of the 2026-06-09 live capture. So no real
  input anchor exists.
- **No display-flip timing**: vkd3d attaches no `VkPresentIdKHR`
  (`last_vulkan_present_id=0`) and the `vkGetLatencyTimingsNV` driver ring
  produces nothing in this configuration (see the latency-meter plan,
  Phase 0/Phase 3-cut). So P2D is not measurable.
- What RE9 **does** emit reliably for the whole session: simulation
  start/end, render-submit start/end, present start/end, and out-of-band
  present markers, at base-frame (pre-FG) cadence.

## Conclusion: the honest Linux metric is FS2P (sim → present)

`sim_to_present_us = SIMULATION_START → PRESENT_END` per present ID, p95 over
the rolling window, displayed in ms. Compared to the Windows PCL number it
omits:

- **I2FS** — with Reflex active, input sampling is deliberately aligned to
  just after simulation start (that is what Reflex's just-in-time sleep
  does), so the missing slice is small (~half the input poll interval).
- **P2D** — present to flip; up to roughly one refresh plus queue depth.

Net effect: reads a few ms lower than the NVIDIA App number for the same
scene. Same semantics family as "instrumented latency" in PresentMon.

### Reflex 2 / Frame Warp caveat

Reflex 2 re-projects (perspective-warps) rendered and frame-generated frames
with the latest mouse input just before scan-out. That latency gain is
**invisible to every marker pipeline** — NVIDIA measures warp gains with
LDAT/photon capture, not PCL Stats. Frame Warp is Windows-only and not in
the dxvk/vkd3d path, so for RE9 on Linux the sim→present number remains
comparable to PCL in non-warp titles.

## Implementation

The metric is computed and shown two ways, by where the markers are observed:

- **Pre-FG / menus — Vulkan layer.** The PenguinBurner Vulkan layer
  (`native/latency_layer/src/penguinburner_latency_layer.cpp`) observes
  forwarded `vkSetLatencyMarkerNV` markers and emits `marker-proxy` samples
  (`sim_to_present_us`, `submit_to_present_us`, `sim_to_oob_present_us`). This
  works only while vkd3d forwards in-band markers — i.e. menus and non-FG
  scenes. Once frame generation clears the low-latency owner, these markers
  stop reaching the layer.

- **In-game / under frame generation — dxvk-nvapi marker tap.** The markers are
  captured above vkd3d's owner-gate (stock dxvk-nvapi trace, or the optional
  marker-tap patch) and bridged into the same receiver path. This is the
  working in-game solution and is documented in full in
  [fg-overlay-latency.md](./fg-overlay-latency.md).

Both feed the same downstream: receiver tier ladder
`input→present` > `marker input→present` > `sim→present` >
`sim→OOB-present` > `submit→present` > `render-submit`; snapshot publishes
`latency_p95_ms` + `latency_quality`; `overlay_state_publisher` writes
`latency_ms`, which the overlay renders after the FPS value, e.g.
`85 FPS 20 ms 2535 MHz 850 mV Bal`. The latency segment is omitted (not "n/a")
when no marker tier is live.

Verified live on RE9 under frame generation (2026-06-11): steady ~20 ms
`sim-to-present`, in the expected 30–60 ms band at low base FPS, value tracking
scene load.

## Sources

- [Understanding and Measuring PC Latency — NVIDIA Technical Blog](https://developer.nvidia.com/blog/understanding-and-measuring-pc-latency/)
- [Streamline PCL Programming Guide](https://github.com/NVIDIAGameWorks/Streamline/blob/main/docs/ProgrammingGuidePCL.md)
- [Streamline Reflex Programming Guide](https://github.com/NVIDIA-RTX/Streamline/blob/main/docs/ProgrammingGuideReflex.md)
- [PresentMon Console Application docs](https://raw.githubusercontent.com/GameTechDev/PresentMon/main/README-ConsoleApplication.md)
- [PresentMon: All-Input-To-Photon metric discussion (#366)](https://github.com/GameTechDev/PresentMon/issues/366)
- [NVIDIA Reflex SDK](https://developer.nvidia.com/performance-rendering-tools/reflex)
- Local: FrameView SDK README (`/home/jp/win/Program Files/NVIDIA Corporation/FrameViewSDK/README.txt`),
  dxvk-nvapi sources (`third_party/dxvk-nvapi`), RE9 live captures
  (`~/.cache/penguin-burner/latency-captures/`)
