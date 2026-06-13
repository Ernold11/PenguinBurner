# Plan: Wayland-free headless Q2RTX via Xvfb

Status: **proposed** (not implemented). Today the headless path is
`gamescope --backend headless`; this document specifies a future migration to
an Xvfb-based path so the stability test can run on a display-less server
without pulling in a Wayland/wlroots stack.

## Motivation

The Q2RTX stability workload (Auto-UV voltage scan and `--stability-test`) needs
a real Vulkan surface, so it cannot run truly windowless against a prebuilt
Q2RTX binary. On a headless server there is no `$DISPLAY`, so we currently
require `gamescope --backend headless`, which spins up its own private offscreen
compositor.

The problem is gamescope's dependency footprint. It is a wlroots-based Wayland
micro-compositor, so installing it drags a large graphics stack onto the server:

| Mechanism | Approx. new packages | Notable pulls | Wayland? |
|-----------|----------------------|---------------|----------|
| **gamescope** | ~25–30 | wlroots, libwayland-client/server, **Xwayland**, ~14 `libX*` client libs, SDL2, pipewire-libs, libinput, libseat, luajit | **yes** |
| **Xvfb** (`xorg-x11-server-Xvfb`) | ~5–8 | xorg-x11-server-common, xorg-x11-xauth, libXfont2, libXau, libXdmcp, pixman, libunwind | **no** |

(Measured on Fedora 43 via `dnf repoquery --requires`.) Xvfb is the X *server*,
not an X client, so it does **not** pull the `libX11`/`libXext`/`libXi` client
cluster, and pulls **no** Wayland, wlroots, pipewire, or Xwayland. It directly
satisfies a "no Wayland on the server" requirement and is a much smaller add.

Both gamescope's libraries and Xvfb are inert on disk — neither starts a desktop
or visible window on its own. Vulkan still renders on the NVIDIA GPU directly;
Xvfb only supplies the X11 window-system protocol the SDL surface needs.

## Goal

On a headless server, run the Q2RTX timedemo workload with:

- **no Wayland/wlroots/Xwayland dependency**,
- **nothing ever displayed**, and
- a clear, fail-fast error when the headless prerequisites are missing (never a
  surprise visible window, never a hang).

Keep the existing visible-window debug mode (`--show-q2rtx-window`) and the
desktop developer experience unchanged.

## Current architecture (what changes)

The launch chain lives in `stability/q2rtx/`:

- `process_harness.py`
  - `_headless_gamescope_prefix(config)` — builds the `gamescope --backend
    headless -W … -H … -- ` command prefix when `hide_window` and gamescope is on
    `PATH`.
  - `_wrap_q2rtx_command(command, gamescope_prefix=…)` — prepends the prefix.
- `gpu_binding.py`
  - `_apply_hidden_window_env(env, hide_window=…, use_headless_gamescope=…)` —
    when *not* using gamescope, forces `SDL_VIDEODRIVER=x11` + an off-screen
    override-redirect window at `HIDDEN_WINDOW_POSITION` (`32000,32000`).
- `models.py`
  - `Q2RTXStabilityConfig.use_headless_gamescope: bool = True`.
- `runtime.py`
  - resolves the prefix, sets `use_headless_gamescope=bool(gamescope_prefix)`,
    and contains the **gamescope-startup-crash auto-retry** (`replace(config,
    use_headless_gamescope=False)`), which drops to the X11 fallback.
- `reporting.py` — surfaces the headless mode in output.
- Tests: `tests/test_q2rtx_stability.py`, `tests/test_docs_cli_flags.py`.

## Proposed design

### 1. Xvfb launch prefix

Replace `_headless_gamescope_prefix` with `_headless_xvfb_prefix(config)`:

```python
def _headless_xvfb_prefix(config):
    if not config.hide_window or not config.use_headless_xvfb:
        return []
    xvfb_run = shutil.which("xvfb-run")
    if not xvfb_run:
        return []
    server_args = f"-screen 0 {int(config.width)}x{int(config.height)}x24 +extension GLX +render -noreset"
    return [xvfb_run, "-a", "-s", server_args, "--"]
```

- `xvfb-run -a` auto-allocates a free display number and exports `DISPLAY` to the
  wrapped process, then tears the virtual server down on exit.
- 24-bit depth + GLX/RENDER extensions are enough for SDL to create an X11
  Vulkan window; the actual rendering is NVIDIA Vulkan on the GPU.
- The virtual framebuffer is never shown anywhere, so this is genuinely
  headless.

### 2. Environment

In `_apply_hidden_window_env`, keep forcing `SDL_VIDEODRIVER=x11` (now pointed at
the Xvfb virtual display). The off-screen window position becomes irrelevant
(the whole display is virtual and unseen) but is harmless; it can be kept or
dropped. The NVIDIA render-offload / device-select env (`_apply_nvidia_render_offload_env`)
is unchanged.

### 3. Strict headless guard

When `hide_window` is set, the harness must stay headless:

- If `xvfb-run` is available → use it (self-contained virtual display).
- Else if an existing `$DISPLAY` is present → allowed (developer desktop; uses
  the real display's off-screen window as today).
- Else → **fail fast** with a clear message:
  `"headless Q2RTX requested but xvfb-run was not found and no $DISPLAY is set —
  install xorg-x11-server-Xvfb"`. Do **not** spawn a visible window, and do
  **not** silently no-op.

Remove the gamescope-startup-crash auto-retry; with Xvfb there is no compositor
to crash, so the retry tier is obsolete.

### 4. Device-lost surfacing (fold in)

Independent but adjacent: when the fatal-output scan matches `device lost` /
`VK_ERROR_DEVICE_LOST`, or an `NVRM: Xid` is detected, print a distinct
`GPU device lost` line and set a specific result reason `gpu-device-lost`
instead of the generic `fatal-q2rtx-output`. (`FATAL_OUTPUT_PATTERNS` in
`constants.py` already detects the strings; this only improves reporting.)

### 5. Config / CLI

- Rename `Q2RTXStabilityConfig.use_headless_gamescope` → `use_headless_xvfb`
  (or a generic `headless: bool`), defaulting to `True`.
- `--show-q2rtx-window` keeps its current meaning (force a visible window,
  disable the headless path).
- Optional: add `PENGUIN_BURNER_HEADLESS=1` / `--headless` to force the strict
  guard even when a `$DISPLAY` happens to be set (useful in CI).

### 6. Packaging / docs

- Add `xorg-x11-server-Xvfb` to `packaging/arch/PKGBUILD` `depends` and
  `packaging/rpm/penguin-burner.spec` `Requires` — **or** keep it optional and
  documented with the strict guard failing fast when missing. Decision pending
  (see Open questions). Xvfb is light enough that a hard `Requires` is far more
  defensible than gamescope's.
- Update `readme-cli.md` "Headless Q2RTX" and `docs/features/troubleshooting.md`
  to describe the Xvfb path and drop the gamescope-server recommendation once
  shipped.

### 7. Tests

- Update `tests/test_q2rtx_stability.py` gamescope cases to the Xvfb prefix.
- Update `tests/test_docs_cli_flags.py`.
- Add: strict-guard fail-fast (no xvfb-run + no `$DISPLAY`), Xvfb prefix shape,
  and the `gpu-device-lost` reason.

## Rollout

`gamescope` and `xvfb` are mutually exclusive mechanisms for the same job; the
intent is to **replace** gamescope, not run both. A conservative rollout could
land Xvfb behind a flag first (`use_headless_xvfb`, gamescope still default),
soak it, then flip the default and remove the gamescope tier in a follow-up.

## Alternatives considered

- **Bundle a private gamescope** (ship wlroots+libwayland+… in the wheel's
  package-data with a private rpath). Removes *system packages* but still ships
  the entire Wayland compositor as files we then own and must security-update;
  big wheel; gamescope/wlroots do not static-link cleanly upstream. Rejected as
  high-maintenance for no real reduction in Wayland code.
- **Patch Q2RTX to use `VK_EXT_headless_surface`** (no compositor, no X, no
  Wayland — the truly minimal path). Rejected for now because we ship a
  **prebuilt** Q2RTX binary (`stability/q2rtx/downloader.py`); this would require
  building and maintaining a Q2RTX fork. Best end-state, highest cost; revisit if
  we ever build Q2RTX from source.
- **Keep gamescope, document it for servers** (the current interim choice).
  Smallest change; accepts the Wayland dependency footprint on servers that opt
  into headless.

## Open questions

1. Xvfb as a hard packaging dependency, or optional + documented + fail-fast?
2. Remove gamescope entirely, or keep it as an opt-in alternative behind a flag?
3. Headless trigger: auto-detect from unset `DISPLAY`/`WAYLAND_DISPLAY`, an
   explicit `--headless`/env flag, or both?
