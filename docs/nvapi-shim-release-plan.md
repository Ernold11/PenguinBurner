# NVAPI shim — release-readiness plan

> **Status 2026-07-01: audited and merged to `main`.** Capture architecture is
> validated (Talos 2, RE9 — both DX12 + Streamline + DLSS-G) and sound.
> **Not ready for average-user release**: one likely showstopper (B1), one
> distribution gap (B2), plus hardening items. This file is the work plan to
> get it there.

## Verdict summary

| # | Finding | Severity | Phase | Status |
|---|---------|----------|-------|--------|
| B1 | No guaranteed FIFO drainer → game freeze when the app is closed | Blocker | 1 | **DONE 2026-07-01** — detached per-game drainer + per-launch FIFO + shim drop-on-full ring. Manual matrix pending. |
| B2 | Shim DLL not built in any release channel (Flatpak/arch/deb/rpm) | Blocker | 2 | **PREPARED 2026-07-01 (all channels, unpublished)** — Flatpak (mingw SDK extension, DLL verified in-app), wheel (EPEL mingw in the cibuildwheel container, DLL verified in-wheel), arch/deb/rpm build-deps, `REQUIRE_NVAPI_SHIM=1` everywhere, marker-source startup diagnostic. Distro packages need one real build each to verify dep names; nothing published yet. |
| H1 | `_file_contains` 1 MB chunk scan can miss the needle → sidecar destruction | High (latent) | 3 | open |
| H2 | Bridge pairs markers by frameID only → cross-game mispairing | High | 3 | largely mooted by per-launch FIFOs (one game per pipe); `(pid, frame)` keying still worthwhile |
| H3 | Test suite red in clean checkout (`VK_LAYER_DXVK_NVAPI_reflex` assertion) | High (CI) | 3 | open |
| M1 | Watcher TOCTOU with Proton's non-atomic DLL copy → truncated sidecar | Medium | 4 | partially fixed 2026-07-01 (`d4caacb`): watcher reacts only to completed rewrites (`IN_CLOSE_WRITE`/`IN_MOVED_TO`); parking sanity-check still open |
| M2 | Fixed 60 s watch window / brand-new prefix gets no shim | Medium | 4 | **DONE 2026-07-01** (`d4caacb`) — inotify watcher scoped to the Proton session (pidfd), no fixed window |
| M3 | `IsBadReadPtr` guard-page hazard | Medium | 4 | open |
| M4 | No un-front/cleanup path (disable/uninstall leaves shim + sidecar) | Medium | 4 | open |
| M5 | Anti-cheat coverage untested (EAC/BattlEye) | Medium | 5 | open |
| L1 | Doc rot: removed marker-log/trace fallback still described | Low | 3 | open |
| L2 | Wrapped games' stderr swallowed by the FIFO (support/debugging) | Low | 5 | open |
| L3 | FIFO path mismatch under explicit `PENGUIN_BURNER_LATENCY_SOCKET` | Low | 4 | mooted: the drainer is told its FIFO explicitly (`--log`, `PENGUIN_BURNER_MARKER_FIFO`) |
| — | Merge to `main` once Phases 1–3 land (shim is the chosen direction) | Gate | 5 | open |

---

## Phase 1 — B1: guarantee a FIFO drainer (game-freeze blocker)

> **Status 2026-07-01: IMPLEMENTED** (plan items 2 + 3, plus per-launch FIFOs).
> The wrapper spawns `nvapi_marker_bridge` as a detached per-game drainer
> (`--session-pid`/`--cleanup`); each launch gets its own
> `nvapi-trace.<sessionpid>.fifo`; the shim's emit path is a ring buffer + writer
> thread that drops on a stalled pipe instead of blocking.
> See docs/nvapi-shim.md "The freeze hazard". Remaining: the manual validation
> matrix below (app closed at launch / closed mid-game / started mid-game).

**Original finding.** Before the detached drainer existed, the only reader lived
inside the app runtime. The wrapper redirected the game's stderr into the FIFO
and the shim wrote markers to it by path. A FIFO's kernel buffer is 64 KB and
the launcher's O_RDWR fd kept it open without consuming; at ~2–5 marker lines
(~100 B) per frame a Reflex title filled it in seconds. Then the shim's blocking
`_write` stalled inside `wrap_set_latency_marker` while holding `g_emit_lock` —
the game's simulation thread froze until the app started and drained the pipe.
The wrapper lives permanently in Steam launch options, so "game launched while
PenguinBurner is closed" is a normal average-user state. `d992f18` made the
redirect default-on for every wrapped launch (the wrapper setdefaults the
overlay to `"auto"`, making `ingame_latency_enabled()` true), so the hazard
went from opt-in to default.

**Plan.**
1. Reproduce first: wrapper + Reflex title + app closed → confirm the freeze
   and time-to-freeze. (Confirms mechanism and gives the regression test.)
2. Fix by making the drainer's lifetime match the game's, not the app's:
   spawn the marker bridge as a detached per-game process from the wrapper
   (same pattern as `spawn_refront_watcher`). It drains the FIFO always and
   forwards samples to the latency socket(s); the app just receives datagrams
   whenever it happens to be running. Remove/keep the in-app bridge behind a
   flag so exactly one reader exists (two readers steal lines from each other).
3. Belt-and-braces regardless of 2: make the shim tolerate a full pipe —
   e.g. periodic short-write/timeout detection or drop-on-full — so a dead
   drainer can never stall the game thread. Never block while holding
   `g_emit_lock`.
4. Tests: unit-test the wrapper spawns the drainer; manual matrix — app
   closed at launch, app closed mid-game, app started mid-game (latency should
   appear live).

**Done when:** a wrapped Reflex title runs indefinitely with the app closed,
and latency appears in the overlay when the app starts afterwards.

## Phase 2 — B2: actually ship the DLL (distribution blocker)

> **Status 2026-07-01: Flatpak SHIPPED.** The manifest pulls
> `org.freedesktop.Sdk.Extension.mingw-w64//25.08` (build-time only,
> `append-path: /usr/lib/sdk/mingw-w64/bin`) and sets
> `PENGUIN_BURNER_REQUIRE_NVAPI_SHIM=1` so a missing toolchain fails the build
> instead of shipping hollow; both build scripts install the extension. Local
> `flatpak-builder` run verified `overlay/nvapi_shim/nvapi64.dll` (PE, needle
> present) inside the installed app. **Wheel/PyPI wired 2026-07-01:**
> `build-python-dist.sh` installs `epel-release` + `mingw64-gcc-c++` +
> `mingw64-winpthreads-static` in the manylinux_2_28 container
> (`CIBW_BEFORE_ALL_LINUX`) and sets `REQUIRE_NVAPI_SHIM=1`; the shim source
> pins `_WIN32_WINNT=0x0601` because EPEL8's MinGW 7.2 otherwise hides
> `InitOnceExecuteOnce`, and needs static winpthreads for the `-static` link.
> **arch/deb/rpm + diagnostic wired 2026-07-01:** PKGBUILD adds
> `mingw-w64-gcc`, debian adds `g++-mingw-w64-x86-64`, the rpm spec adds
> `mingw64-gcc-c++` + `mingw64-winpthreads-static`; all three export
> `PENGUIN_BURNER_REQUIRE_NVAPI_SHIM=1`. The app's telemetry startup log now
> names the active marker source (`nvapi shim (<path>)` vs `Vulkan layer only
> -- shim DLL missing`), so degraded installs show in bug reports. All
> preparation only -- no channel has been published with the shim yet; the
> distro packages still need a real build each to verify the dep names.

**Finding.** No release channel builds the shim:
- Flatpak: freedesktop 25.08 SDK has no `x86_64-w64-mingw32-g++`; the manifest
  sets `PENGUIN_BURNER_REQUIRE_NATIVE_LAYER=1` but not
  `PENGUIN_BURNER_REQUIRE_NVAPI_SHIM`, so the build warns and ships without
  the DLL — silent degrade to the layer tap, i.e. no FG-title latency on the
  primary channel.
- `packaging/arch`, `packaging/debian`, `packaging/rpm`: no mingw build-dep.
- Nothing user-visible reports which marker source is active, so the miss is
  silent.

**Plan.**
1. Decide build strategy: (a) add mingw-w64 to every build environment
   (Flatpak needs a mingw toolchain module in the manifest — heavy but
   hermetic), or (b) build the DLL once in CI and ship it as a versioned,
   checksummed artifact consumed by all packagings (the DLL is
   Proton-version-independent by design, ~241 KB, static). Lean (b);
   keep `build.sh` as the reproducibility recipe.
2. Set `PENGUIN_BURNER_REQUIRE_NVAPI_SHIM=1` in all release builds so a
   missing toolchain/artifact fails the build loudly instead of shipping a
   hollow feature.
3. Add mingw-w64 build-deps to arch/deb/rpm packaging (or wire them to the CI
   artifact per 1).
4. Surface the active marker source in the app UI/diagnostics
   (`shim | layer | none`), so a degraded install is visible in bug reports.
5. CI check: built package must contain `overlay/nvapi_shim/nvapi64.dll`.

**Done when:** a fresh install from each channel deploys the shim on a real
prefix (log shows `nvapi shim: installed …`).

## Phase 3 — correctness + hygiene (before release)

- **H1 — chunk-boundary needle miss** (`overlay/shim_deploy.py:_file_contains`):
  scan reads 1 MB chunks without overlap; a needle straddling a boundary is
  missed. Today impossible (DLL is 241 KB, single chunk), but if the DLL ever
  exceeds 1 MB, a false negative parks *the shim itself* as `nvapi64-pb.dll` —
  destroying the real dxvk-nvapi and making the shim forward to itself
  (infinite recursion → stack overflow in the game). Fix: overlap chunks by
  `len(needle) - 1`. One line + test.
- **H2 — cross-game mispairing** (`overlay/telemetry/nvapi_marker_bridge.py`):
  the PID is parsed from each line but `pending_sim`/`pending_input`/
  `awaiting_oob` are keyed by frameID only; two concurrently wrapped games
  cross-pair frames. Fix: key all pairing state by `(pid, frame)`.
- **H3 — red test in clean checkout**:
  `test_pb_overlay_launcher_execs_with_layer_environment` asserts
  `VK_LAYER_DXVK_NVAPI_reflex`, which requires the untracked
  `third_party/dxvk-nvapi/build.layer` (dev leftover from the rejected-fork
  approach, superseded by the shim). Remove the dead layer wiring
  (`launcher.py:70` `_DXVK_NVAPI_LAYER_DIR` + `_prepend_*` branches) and the
  assertion.
- **L1 — doc rot**: `shim_deploy.py` module docstring, `setup.py:135`, and the
  bridge module docstring still describe the removed dxvk-nvapi
  marker-log/trace fallback; the actual fallback is the Vulkan layer tap.
  Update alongside `docs/nvapi-shim.md`.

## Phase 4 — robustness hardening (release-adjacent, can fast-follow)

- **M1 — watcher TOCTOU**: Proton's DLL sync isn't atomic; the watcher can
  classify a half-written `nvapi64.dll` as stock and park a truncated sidecar.
  Self-heals next launch (LoadLibrary fails → game sees no NVAPI that run) but
  yields "latency randomly missing" reports. Mitigation: sanity-check the
  candidate before parking (PE header + plausible size, or stable-size
  double-read), and never overwrite a healthy sidecar with a smaller file.
- **M2 — watch window / first launch**: slow first launches (prefix creation,
  shader precompile) can outlast the fixed 60 s window, and a brand-new prefix
  gets no shim at all (deploy and watcher both bail before `pfx/` exists).
  Mitigation: extend the watch until the game process is observed (or prefix
  appears + one re-front), keep the env override as the escape hatch.
- **M3 — `IsBadReadPtr`** (`nvapi_shim.cpp:240,248`): can consume a thread's
  stack guard page and cause an unrelated crash later. Callers (Streamline,
  dxvk) pass valid structs and the real nvapi does a plain null check — the
  probe adds more risk than it removes. Replace with the null check alone (or
  a VEH-scoped probe if paranoia is required).
- **M4 — cleanup/un-front path**: `PENGUIN_BURNER_NVAPI_LATENCY_DISABLE` stops
  re-deploying but never restores the prefix; uninstall leaves shim + sidecar
  behind. Works today only because Proton re-clobbers per launch. Add a
  `--restore` mode to `overlay/shim_deploy.py` (sidecar → `nvapi64.dll`,
  delete sidecar) and call it from disable/uninstall docs.
- **L3 — FIFO path mismatch**: with an explicit `PENGUIN_BURNER_LATENCY_SOCKET`
  the bridge derives the FIFO next to that socket while the launcher always
  uses `~/.cache/penguin-burner/`. Unify on one derivation.

## Phase 5 — release gates (decisions + validation matrix)

- **M5 — anti-cheat matrix**: RE9's anti-tamper tolerated the proxy and Wine
  prefixes are unsigned DLLs everywhere, but no EAC/BattlEye title has been
  tested. Before default-on release: test ≥1 EAC and ≥1 BattlEye title;
  document the per-game opt-out (`PENGUIN_BURNER_NVAPI_LATENCY_DISABLE=1`)
  prominently either way.
- **L2 — swallowed stderr**: wrapped games' console/stderr output disappears
  into the FIFO (discarded by the bridge). `PROTON_LOG=1` file logging still
  works; add a support-doc note, optionally tee non-marker lines when a debug
  env is set.
- **Branch policy**: the shim is the chosen direction and the old
  `nvapi_shim` branch has been merged to `main`; keep future work on `main` or
  a fresh topic branch.
- **Validation matrix (re-run before tagging)**:
  - FG + Streamline (Talos 2, RE9) → `quality=reflex-marker-sim-present`,
    sane sim→oob-present.
  - Single-swapchain Reflex title → layer tap and shim coexist, meter picks
    best tier, no double-count artifacts.
  - Non-NVAPI title → no shim deploy, no regressions.
  - App closed at launch / closed mid-game / started mid-game (Phase 1 gate).
  - Fresh prefix first launch, second launch (M2).
  - Flatpak, wheel, and one distro package install end-to-end (Phase 2 gate).

## Explicitly out of scope (tracked in docs/nvapi-shim.md "Remaining")

- `GetLatency` frame-report harvest (more timing tiers).
- 32-bit titles (`nvapi.dll` in syswow64).
- Gating the benign shim/layer marker double-capture.
