# Frame-Generation In-Game Latency on the PenguinBurner Overlay

Date: 2026-06-11

How the PenguinBurner Vulkan overlay shows real Reflex `sim-to-present` latency
during DLSS **frame generation** on Linux/Proton, alongside the pre-frame-gen
present FPS — and why it is an opt-in.

Related:
[pc-latency-windows-tools-findings.md](./pc-latency-windows-tools-findings.md)
(how NVIDIA App / PresentMon measure PCL),
[re9-latency-experiment-findings.md](./re9-latency-experiment-findings.md),
[patches/](./patches/).

## The problem

The overlay's latency number is Reflex `sim-to-present` = time from
`SIMULATION_START` to `PRESENT_END` for a base frame (the FS2P component of
NVIDIA's PC Latency). The markers travel
game → Streamline (`sl.reflex`) → dxvk-nvapi (`nvapi64.dll`) →
vkd3d-proton `ID3DLowLatencyDevice::SetLatencyMarker`.

vkd3d-proton forwards in-band markers only to the device's **low-latency
swapchain owner**, and it **clears that owner once frame generation starts a
second Vulkan swapchain** (verified live on RE9, 2026-06-11: `owner=0`,
`vk_swapchain_count=2`). After that the in-band markers are issued by the game
every frame but dropped before reaching any Vulkan layer — so the PenguinBurner
Vulkan layer (and any `LD_PRELOAD`) is blind to them during FG. Out-of-band
present markers survive (re-emitted on the present thread to the live
swapchain), which is why **pre-FG present FPS keeps working** under FG but
latency does not.

Confirmed dead ends on this stack: the `vkGetLatencyTimingsNV` driver ring
returns nothing/stale; `VkPresentIdKHR` is absent (no present-wait
correlation); `LD_PRELOAD`/Vulkan-layer taps sit below the owner-gate.

## What works: capture above the owner-gate

The markers are still observable one level up, at dxvk-nvapi's
`NvAPI_D3D_SetLatencyMarker`, before vkd3d's owner-gate drops them. Stock
dxvk-nvapi already logs every marker there at trace level
(`DXVK_NVAPI_LOG_LEVEL=trace`), so **no custom DLL is required**.

Pipeline:

1. `DXVK_NVAPI_LOG_LEVEL=trace` + `PROTON_LOG=1` → stock `nvapi64.dll` writes a
   line per Reflex marker (`frameID`, `markerType`) to the Proton log.
2. `latency_telemetry/nvapi_marker_bridge.py` tails that log, pairs
   `SIMULATION_START`→`PRESENT_END` by frame id, and sends a `marker-proxy`
   timing sample (`sim_to_present_us`) to the latency socket.
3. The existing receiver → `overlay_state_publisher` → Vulkan overlay path
   renders it as `latency_ms` (tier `sim-to-present`).

Verified live on RE9 under frame generation: steady ~20 ms `sim-to-present`,
overlay line `85 FPS 20 ms 2535 MHz 850 mV Bal`, bridge 0.3% CPU, value tracks
load (12 ms light scene → 23 ms heavy).

## Opt-in and overhead

In-game latency is **off by default** because trace logging is heavy: stock
dxvk-nvapi trace logs *every* NvAPI call (~5–8k lines/sec), not just markers.
The default overlay (present FPS, clocks, voltage, menu-only latency) runs on
stock Proton with zero trace overhead.

Enable / disable:

```bash
# enable in-game latency (adds the toggle + installs the bridge service)
penguin-burner-steam-game-setup --game re9 --experimental-ingame-latency
# disable (default: trace-free, removes the bridge service)
penguin-burner-steam-game-setup --game re9
```

Mechanics:

- The Steam launch line is just the wrapper:
  `PENGUIN_BURNER %command% /WineDetectionEnabled:False`. The
  `PENGUIN_BURNER` wrapper (`penguin_burner_overlay/launcher.py`) sets all the
  Vulkan/NVAPI env. With the flag, the line gains
  `PB_INGAME_LATENCY=1`, which tells the wrapper to *also* enable
  `DXVK_NVAPI_LOG_LEVEL=trace` + `PROTON_LOG`. Without it, the wrapper stays
  trace-free.
- The flag installs/enables `pb-latency-bridge.service` (user unit,
  `Restart=always`) that runs the bridge; the default path removes it.

### Where the overhead lives

The trace cost is in the **game process**: dxvk-nvapi formats and writes a line
for every NvAPI call. Filtering on the read side cannot reduce it — the bytes
are already produced. Two levers actually cut it, both upstream:

- **Disk:** route the trace to an in-memory pipe instead of `PROTON_LOG`'s
  growing `~/steam-<appid>.log` (a FIFO the bridge drains). Removes the file and
  disk I/O; the per-line formatting cost remains. (Design noted; not yet wired.)
- **CPU/volume:** log only latency markers instead of all NvAPI calls — needs a
  small dxvk-nvapi change. See
  [patches/dxvk-nvapi-latency-marker-tap.patch](./patches/dxvk-nvapi-latency-marker-tap.patch),
  the env-gated `DXVK_NVAPI_LATENCY_MARKER_TRACE` tap (~hundreds of lines/sec
  vs ~8k).
  This is the lowest-overhead route and a candidate upstream PR; it requires
  building a custom `nvapi64.dll` per Proton's dxvk-nvapi version, which is why
  the trace path is the default for the feature.

## Components

- `penguin_burner_overlay/launcher.py` — `PENGUIN_BURNER` wrapper; computes all
  env (layers, NVAPI, socket, overlay paths) and gates trace behind
  `PENGUIN_BURNER_INGAME_LATENCY`.
- `latency_telemetry/nvapi_marker_bridge.py` — tails the marker log (stock
  trace lines or the `LAT` marker-tap lines), pairs sim→present, feeds the
  socket.
- `latency_telemetry/steam_game_setup.py` — `--experimental-ingame-latency`
  flag; builds the launch line and manages the bridge service.
- `latency_telemetry/receiver.py` — `sim-to-present` tier, `latency_p95_ms` /
  `latency_quality` in the snapshot.
- `runtime_gpu_control/overlay_state_publisher.py` /
  `penguin_burner_overlay/state.py` — `latency_ms` field through to the overlay.

## Not the chosen path (recorded)

A vkd3d-proton patch to keep/transfer the Reflex owner under FG was tried and
rejected: retaining the owner deadlocks (`LatencySleep` waits on a
non-presenting swapchain), skipping the sleep crashes, and pairing a custom
`d3d12core.dll` with Proton's stock `d3d12.dll` stub crashes on an ABI
mismatch. See
[patches/vkd3d-proton-re9-allow-multi-swapchain-reflex.patch](./patches/vkd3d-proton-re9-allow-multi-swapchain-reflex.patch)
for the diagnostic patch and its failure analysis. The dxvk-nvapi-level capture
above is the working approach and needs no vkd3d changes.
