# PB Vulkan Overlay Design

## Goal

Add a PenguinBurner-owned Vulkan/Proton overlay enabled from Steam launch options with:

```text
PB_OVERLAY %command%
```

The overlay shows only three live values in the top-right corner:

```text
Base 54 FPS
GPU 2760 MHz
V 875 mV
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

Default placement is top-right. Text is right-aligned, readable on 1080p through 4K, and drawn in white with a black outline or shadow. The first implementation should avoid panels and decorative UI unless live testing proves a subtle translucent backdrop is required for readability.

## Data Sources

Base FPS is computed inside the Vulkan layer from present-to-present intervals already observed in `vkQueuePresentKHR`. The layer keeps a short rolling window of positive `present_frametime_us` samples and reports FPS from the median frametime so a single hitch does not crater the displayed number.

The label is `Base FPS` because current RE9 FG x3 captures showed `present-fps=54` mapping to about 162 generated/displayed FPS. This makes the signal useful as pre-frame-generation base-frame cadence for that stack. It must not be labeled GPU render latency.

GPU clock and voltage come from a PenguinBurner metrics provider outside the game process. The provider samples the selected GPU with existing PenguinBurner code:

- NVML graphics clock through `NvmlRuntimeSession` / `live_gpu_telemetry_text`.
- Undocumented NVIDIA voltage through `HiddenNvapiVoltageReader`.

The provider writes a tiny local shared state that the Vulkan layer can read without blocking. If the provider is absent or stale, the overlay displays `GPU n/a` or `V n/a` while continuing to show base FPS.

## Architecture

Extend the existing native Vulkan layer rather than depending on MangoHud. MangoHud's source confirms the mature shape: hook swapchain creation and `vkQueuePresentKHR`, record an overlay draw command before present, submit it, and make present wait on the overlay semaphore. PenguinBurner should implement only the narrow subset needed for fixed text.

Components:

- `PB_OVERLAY` launcher: sets overlay and layer environment for Steam/Proton commands.
- Overlay metrics provider: publishes current GPU clock and voltage for the selected GPU.
- Vulkan layer overlay state: stores rolling base-FPS samples and latest external metrics.
- Tiny text renderer: draws three fixed text rows on the swapchain image before present.

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
3. Build the three-line overlay text.
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
Base n/a FPS
GPU n/a MHz
V n/a mV
```

and normal numeric values.

## Staleness And Fallbacks

Base FPS is `n/a` until enough present intervals exist. Clock and voltage are `n/a` if the shared state is missing, malformed, from another GPU, or older than the configured stale threshold.

The metrics provider should write atomically, for example via a temp file and rename or a small datagram/socket update. The Vulkan layer must never block on Python or GPU APIs from inside the game process.

## Testing

Python tests:

- launcher environment formatting for `PB_OVERLAY`;
- metrics state serialization, parsing, and stale handling;
- selected GPU index propagation;
- expected display string formatting.

Native tests:

- base-FPS rolling median math;
- text layout sizing and right alignment;
- overlay enable/disable environment parsing.

Manual verification:

- `vkcube` or another simple Vulkan app;
- one Proton title at 1080p;
- one Proton title at 4K or scaled 4K;
- provider absent, provider stale, and provider active;
- overlay failure does not prevent the game from presenting.

## Out Of Scope

- OpenGL overlays.
- MangoHud integration or dependency.
- Graphs, frametime history, percentiles, benchmark logging, hotkeys, or presets.
- In-game configuration UI.
- Calling undocumented NVAPI voltage APIs from inside the Vulkan layer.
