# Running PenguinBurner under gamescope

How to combine the PenguinBurner (PB) latency/overlay Vulkan layer with a
gamescope wrapper in a Steam launch option, why the ordering matters, and what
the latency numbers mean once gamescope is in the present path.

## TL;DR — the launch option

```
PENGUIN_BURNER_LATENCY_DISPLAY=1 PENGUIN_BURNER_OVERLAY=1 PB_OVERLAY=1 PB_INGAME_LATENCY=1 gamescope -W 3840 -H 2160 -r 120 --adaptive-sync -f -- PENGUIN_BURNER %command% /WineDetectionEnabled:False
```

- `gamescope … --` wraps the game; **`PENGUIN_BURNER` goes *after* `--`**, wrapping
  `%command%`.
- Add `--hdr-enabled` to gamescope only **after** it launches clean — HDR on
  gamescope nested on NVIDIA is the usual first-run failure.
- `/WineDetectionEnabled:False` is a game argument and stays at the very end.

## Why the ordering matters

`PENGUIN_BURNER` is an installed wrapper executable
(`~/.local/bin/PENGUIN_BURNER`) that runs `overlay.launcher:main`. The launcher
(`overlay/launcher.py`) sets the Vulkan-layer environment and then **execs its
first argument**:

```python
# overlay/launcher.py
VK_LAYER_PATH_ENV = "VK_ADD_IMPLICIT_LAYER_PATH"
VK_LAYER_ENABLE_ENV = "VK_LOADER_LAYERS_ENABLE"
...
os.execvpe(args[0], args, env)   # execs args[0]
```

So whatever follows `PENGUIN_BURNER` is what gets the layer forced into it.

- ❌ `… PENGUIN_BURNER gamescope … -- %command%` execs **gamescope** with the PB
  layer injected into gamescope itself. The PB layer then fights gamescope's own
  WSI layer over the swapchain — gamescope typically fails to start.
- ✅ `… gamescope … -- PENGUIN_BURNER %command%` runs gamescope normally, and the
  PB wrapper injects the layer only into the **game** inside gamescope.

The canonical, gamescope-free launch option is
`PENGUIN_BURNER %command%` (`DEFAULT_LATENCY_LAYER_LAUNCH_OPTIONS` in
`latency_telemetry/layer_check.py`). Adding gamescope just slots `gamescope … --`
in front of that.

## Env tokens (canonical)

These are the exact tokens the in-app setup emits
(`latency_telemetry/steam_game_setup.py`), so prefer them over guesses:

| Purpose | Tokens |
| --- | --- |
| Overlay | `PENGUIN_BURNER_OVERLAY=1 PB_OVERLAY=1` (`OVERLAY_TOKENS`) |
| In-game latency (dxvk-nvapi trace) | `PB_INGAME_LATENCY=1` (`INGAME_LATENCY_TOKENS`) |
| Display (present→scanout) latency | `PENGUIN_BURNER_LATENCY_DISPLAY=1` |

Present-id injection is already on by default (the layer injects when the game
supplies no present id), so `PENGUIN_BURNER_LATENCY_INJECT_PRESENT_ID=1` and the
extra `…_DEBUG_FLOW=1` are not needed for a normal run.

Env-var prefixes set **before** `gamescope` propagate to both gamescope and the
game (they are inherited). The Vulkan-layer env (`VK_ADD_IMPLICIT_LAYER_PATH`,
`VK_LOADER_LAYERS_ENABLE`) is set by the `PENGUIN_BURNER` wrapper itself, so with
the wrapper after `--` only the game loads the layer.

## gamescope flags for a 4K/120/VRR/HDR display

```
-W 3840 -H 2160     native output size
-r 120              nested refresh (matches a 119.88 Hz mode)
--adaptive-sync     VRR passthrough
-f                  fullscreen
--hdr-enabled       HDR out (add after it runs; needs the gamescope WSI layer)
-O HDMI-A-1         prefer a specific connector (optional)
--backend sdl       fallback if the auto (wayland) backend won't start on NVIDIA
```

Nested inside a KDE Wayland session, gamescope auto-selects the **wayland**
backend. The lowest-latency `--backend drm` requires gamescope to own the
display (run from a TTY as the session compositor) — out of scope for a quick
in-session test.

## Caveats

- **DLSS3 Frame Generation + gamescope WSI on NVIDIA is unverified.** gamescope
  intercepts the present path; FG/Reflex/Streamline pacing may not pass through.
  Launch and confirm FG still works before trusting the result.
- **Measurement changes under gamescope.** With `PENGUIN_BURNER` inside
  gamescope, the layer measures the **game → gamescope** (nested) present, not
  gamescope → KWin → panel. So the display-latency number is *not*
  apples-to-apples with bare runs — it reflects gamescope's nested pacing.
- If HDR looks wrong or the game won't start, drop `--hdr-enabled` first, then
  gamescope entirely.

## Why bother — the display-latency findings that motivated this

Measured on a 3840x2160 @ 119.88 Hz display, VRR "Always", HDR on (KDE Wayland /
KWin, NVIDIA):

| | RE9 (fixed 2× FG) | 007 (adaptive FG) |
| --- | --- | --- |
| Vulkan present mode / queue | FIFO / 4 | FIFO / 4 (identical) |
| display-latency p95 | flat **~1.4 ms** | bimodal, **~2 ms ↔ ~16.7 ms** |

`16.7 ms = exactly 2 × 8.34 ms` (two 120 Hz frames) — the textbook **compositor**
latency (one frame to composite + one to scan out). Both games request the same
FIFO present mode, so the difference is **display-side**: RE9 is being
**direct-scanned-out** by KWin (VRR immediate flip → ~1.4 ms), while 007's
surface is being **composited** by KWin for much of the time (no per-surface VRR
→ the 2-frame path). The in-game "vsync off"/VRR setting only takes effect on
the direct-scanout path, which a composited (borderless/windowed) surface does
not get.

Suspected trigger for 007 being composited: **HDR** (KWin must composite to
tone-map/convert a non-matching plane) and/or window mode. Cheapest test:
toggle HDR off and watch the display term collapse toward ~1–2 ms.

gamescope is the structural fix: it hands KWin a **single fullscreen surface**
that is far more likely to be direct-scanned-out (and can pass through VRR/HDR),
bypassing KWin's per-window compositing of a borderless game — provided FG
survives the nested present path.

## Frame-generation detection note

`framegen_active` on the overlay is driven by the displayed-vs-base **cadence
ratio** (≥ 1.5×). Under **adaptive** FG the multiplier floats, so the flag
flickers off when the game runs near-native — that is expected, not a bug. Fixed
2× stays above the threshold and reads active steadily. See
`docs/` / the receiver's `_framegen_active_for_overlay` for details.
