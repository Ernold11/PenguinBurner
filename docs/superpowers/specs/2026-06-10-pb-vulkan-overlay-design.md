# PB Vulkan Overlay Design

## Goal

Add a PenguinBurner-owned Vulkan/Proton overlay enabled from Steam launch options with:

```text
PB_OVERLAY %command%
```

The overlay shows one compact live line in the top-right corner:

```text
54 FPS 2760 MHz 875 mV Balanced
```

The first version is intentionally small: no plots, no history UI, no MangoHud dependency, no OpenGL support, and no large configuration surface.

## User Experience

`PB_OVERLAY` is a launcher wrapper or command alias that expands to the environment needed for the existing PenguinBurner Vulkan layer and overlay mode:

```bash
PENGUIN_BURNER_LATENCY_LAYER=1 PENGUIN_BURNER_OVERLAY=1 %command%
```

The launcher may also include the existing latency-layer defaults needed for Proton/NVAPI when appropriate. The overlay can still be disabled with:

```bash
PENGUIN_BURNER_OVERLAY=0
```

Default placement is top-right. Text is right-aligned, readable on 1080p through 4K, and drawn in white with a black outline or shadow. The first implementation should avoid panels and decorative UI unless live testing proves a subtle translucent backdrop is required for readability. Adaptive mode does not add a marker to the overlay; the line stays:

```text
54 FPS 2760 MHz 875 mV Performance
```

## Data Sources

Base FPS is computed inside the Vulkan layer from present-to-present intervals already observed in `vkQueuePresentKHR`. The layer keeps a short rolling window of positive `present_frametime_us` samples and reports FPS from the median frametime so a single hitch does not crater the displayed number.

The displayed FPS has no `Base` label to keep the overlay compact, but semantically it is the base-frame cadence. Current RE9 FG x3 captures showed `present-fps=54` mapping to about 162 generated/displayed FPS. This makes the signal useful as pre-frame-generation base-frame cadence for that stack. It must not be labeled GPU render latency.

GPU clock and voltage come from a PenguinBurner metrics provider outside the game process. The provider samples the selected GPU with existing PenguinBurner code:

- NVML graphics clock through `NvmlRuntimeSession` / `live_gpu_telemetry_text`.
- Undocumented NVIDIA voltage through `HiddenNvapiVoltageReader`.

The provider writes a tiny local shared state that the Vulkan layer can read without blocking. If the provider is absent or stale, the overlay displays `n/a` for the missing value while continuing to show base FPS.

## Architecture

Extend the existing native Vulkan layer rather than depending on MangoHud. MangoHud's source confirms the mature shape: hook swapchain creation and `vkQueuePresentKHR`, record an overlay draw command before present, submit it, and make present wait on the overlay semaphore. PenguinBurner should implement only the narrow subset needed for fixed text.

Components:

- `PB_OVERLAY` launcher: sets overlay and layer environment for Steam/Proton commands.
- Overlay metrics provider: publishes current GPU clock and voltage for the selected GPU.
- PenguinBurner daemon: owns the active UV tier/profile decision and publishes the active tier label.
- Vulkan layer overlay state: stores rolling base-FPS samples and latest external metrics.
- Tiny text renderer: draws one fixed text line on the swapchain image before present.

For v1, avoid ImGui. Use a small embedded bitmap font or build-generated monochrome atlas and draw only the glyph quads needed for ASCII labels and numbers.

## Rendering

On `vkCreateSwapchainKHR`, allocate per-swapchain overlay resources:

- image views/framebuffers or dynamic-rendering equivalents for the swapchain format;
- command pool and per-image command buffers;
- semaphores/fences for overlay submit synchronization;
- font texture, sampler, descriptor set, pipeline layout, and simple alpha-blend pipeline.

On `vkQueuePresentKHR`:

1. Update the present interval window and base-FPS text.
2. Read the latest non-stale metrics provider state.
3. Build the one-line overlay text.
4. Record a small command buffer that draws the text in the top-right corner.
5. Submit the command buffer on the graphics queue.
6. Add the overlay semaphore to the present wait path.

If any overlay resource creation or submission fails, the layer logs a single status event and disables visual overlay for that swapchain. It must not block game presentation or latency telemetry.

## Layout

Default font size:

- about 18 px at 1080p;
- scale by framebuffer height;
- cap the scale so 4K remains readable without becoming visually dominant.

Use a fixed margin from the top-right edge. Text should be right-aligned so value width changes do not shift the anchor. The layout only needs enough width for:

```text
n/a FPS n/a MHz n/a mV Performance
```

and normal numeric values.

## UV Profile Tiers

Saved Auto-UV profiles become first-class runtime tiers:

```text
Efficiency < Balanced < Performance
```

Each saved profile stores the tier that generated it:

- Efficiency scans create `Efficiency` profiles.
- Normal/default balanced scans create `Balanced` profiles.
- Performance Auto-OC / performance mode creates `Performance` profiles.

The user can right-click a saved profile and pin it to a tier when multiple profiles exist or when they want to override the generated tier. A profile can be explicitly pinned to only one tier; pinning it to a new tier removes the old explicit tier assignment.

Tier resolution:

- If a tier has a pinned profile, use that profile.
- If a tier has no pinned profile, use the latest verified profile generated for that tier.
- If only one tier/profile exists, PB can use that profile for missing tiers, but adaptive switching is disabled because there is only one real behavior available.
- Adaptive switching only considers tiers that resolve to distinct real profiles. It must not switch to a tier that is only an alias to another tier's profile.

The daemon publishes the active resolved tier label as one of `Efficiency`, `Balanced`, or `Performance`. When daemon overlay state is present, the overlay always displays one of those labels and never displays `Latest` or an adaptive marker.

## Adaptive Tier Switching

Adaptive switching is opt-in. Add an `Adaptive` checkbox next to the UI control where the user enables PenguinBurner UV autostart / selects the autostart profile tier. Adaptive is enabled only when PB UV autostart is enabled. If autostart UV is off, Adaptive is disabled and ignored.

Adaptive requires at least two real/resolvable tiers. One tier may be missing; PB can still switch between the two available real tiers. With only one real tier, PB stays fixed.

The control signal is `present-frametime-p95`, not median FPS. The target is `16.6 ms`, equivalent to 60 base FPS. The overlay can continue showing median-derived FPS, but switching uses p95 because it catches bad pacing.

Recommended fixed bands:

```text
comfort:      p95 <= 14.5 ms
target-ok:    14.5 < p95 <= 16.6 ms
near-slow:    16.6 < p95 <= 18.5 ms
clearly-slow: p95 > 18.5 ms
badly-slow:   p95 > 22.0 ms
```

Promotion rules:

- `badly-slow`: jump directly to the highest available tier.
- `clearly-slow`: move up one tier.
- `near-slow`: move up one tier only after several consecutive windows, initially 3.
- If already on the highest available tier, stay there.

Demotion rules:

- Prefer the lowest available tier that still satisfies the target.
- Demote one tier only after a longer stable period, initially 6 consecutive comfort windows.
- Never demote on a single good window.
- After reaching `Performance`, make it very sticky: require sustained comfort and the minimum dwell time before demoting, and demote only one tier at a time.
- If `Efficiency` is comfortably below target, stay on `Efficiency` unless p95 becomes slow for multiple windows.

Anti-flap rules:

- Minimum dwell time after any switch: initially 60 seconds.
- Promotion can override dwell time only for `badly-slow`.
- Demotion never overrides dwell time.
- At transition edges, prefer the lowest tier whose p95 is enough rather than chasing extra FPS.

## Staleness And Fallbacks

Base FPS is `n/a` until enough present intervals exist. Clock and voltage are `n/a` if the shared state is missing, malformed, from another GPU, or older than the configured stale threshold. In normal daemon operation the tier resolves to `Efficiency`, `Balanced`, or `Performance`; if the daemon is unavailable, `PB_OVERLAY` logs a warning and the visual overlay may launch with FPS-only data.

The metrics provider should write atomically, for example via a temp file and rename or a small datagram/socket update. The Vulkan layer must never block on Python or GPU APIs from inside the game process.

## Testing

Python tests:

- launcher environment formatting for `PB_OVERLAY`;
- metrics state serialization, parsing, and stale handling;
- selected GPU index propagation;
- expected display string formatting;
- profile tier generation, pinning, uniqueness, and latest-within-tier fallback;
- adaptive enablement requiring UV autostart and at least two real tiers;
- adaptive promotion/demotion bands, dwell time, and no-alias switching.

Native tests:

- base-FPS rolling median math;
- text layout sizing and right alignment;
- overlay enable/disable environment parsing.

Manual verification:

- `vkcube` or another simple Vulkan app;
- one Proton title at 1080p;
- one Proton title at 4K or scaled 4K;
- provider absent, provider stale, and provider active;
- one, two, and three available UV tiers;
- adaptive disabled, enabled, promotion, and sticky Performance demotion;
- overlay failure does not prevent the game from presenting.

## Later Ideas

- Per-game adaptive persistence can remember the last stable tier or learned preference for a specific Steam app / Proton title. Leave this out of v1 because app identity and persistence policy can be confusing if detection is wrong.

## Out Of Scope

- OpenGL overlays.
- MangoHud integration or dependency.
- Graphs, frametime history, percentiles, benchmark logging, hotkeys, or presets.
- In-game configuration UI.
- Calling undocumented NVAPI voltage APIs from inside the Vulkan layer.
- Per-game adaptive persistence.
