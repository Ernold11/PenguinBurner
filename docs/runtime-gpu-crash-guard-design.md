# Runtime GPU Crash-Guard for Applied Auto-UV Profiles

**Date:** 2026-07-02
**Status:** Proposed (draft — not scheduled)

## Summary

Auto-UV protects the GPU **during the scan** (the frame-progress hang watchdog,
the unsafe-voltage blacklist, and the confirm-by-reprobe path). It does nothing
**after** a profile is applied and the user is actually gaming. A curve that
passed the Q2RTX probe can still be marginally unstable on a different real
workload and crash or throw an Xid mid-game, with no protection and no memory of
it.

This design adds a runtime crash-guard that watches for GPU instability while an
Auto-UV profile is live, reacts conservatively (raise voltage a step, or drop to
a safer tier), and remembers the safe point so a repeat does not keep happening.
It reuses infrastructure we already have and inherits the same hard bias against
false positives as the scan-time watchdog: **never over-correct a stable profile
over a transient blip.**

---

## Problem

- The scan validates stability against one synthetic RT/compute workload for a
  bounded duration. Real games stress the GPU differently (memory patterns,
  power transients, longer soak) and at a lower voltage floor the curve can be
  *scan-stable but game-unstable*.
- Today a mid-game crash/Xid is invisible to us: the user just crashes, relaunches,
  and hits the same wall. The applied profile is never adjusted.
- We already ship an adaptive runtime controller that changes the live curve for
  **FPS** reasons — but nothing changes it for **stability** reasons.

## Goal

While an Auto-UV profile is active, detect GPU instability, step to a safer
operating point, and persist that so the next session starts safe — without
regressing a genuinely stable profile on a one-off event.

## Non-goals

- Per-game profile databases / per-title tuning. We keep this **profile-scoped
  and generic** (any GPU app), not a per-game cloning feature.
- Replacing the scan. This is a safety net on top of a scanned profile, not a
  substitute for scanning.

---

## What we already have (reuse, do not rebuild)

| Piece | Where | Reuse for |
|---|---|---|
| Xid reader (timestamp-filtered) | `stability/q2rtx/runtime.py` `_query_xid_messages_since` | detection source at runtime |
| Adaptive runtime controller (already swaps tier curves live) | `runtime/gpu_control/adaptive_profile_runtime.py` `AdaptiveAutoUvRuntimeController` | the polling loop + curve-apply path |
| Tier curves + safer-tier ladder | `auto_uv/scan_mode/uv_limits.py`, `auto_uv/run/crash_recovery.py` | the reaction ladder |
| Confirm-before-acting discipline | `auto_uv/q2rtx/q2rtx_cuda_voltage_probe.py` `run_probe_with_hang_confirmation` | false-positive philosophy |
| Live V/F apply | `auto_uv/gpu/gpu_vf_curve_applier.py`, NVML/NVAPI helpers | applying a downstep |

The controller already runs a periodic `update()` and owns curve application, so
the guard is mostly a **new trigger + reaction ladder**, not new infrastructure.

---

## Design

### Detection

- Poll Xid on the controller's existing tick (a few seconds), filtered to the
  current session start time (same helper the scan uses).
- **Classify severity.** Only act on the GPU-instability class; ignore benign /
  app-only Xids. Draft mapping (to be validated on real crashes in Phase 1):
  - Act: engine/exception + MMU/page-fault + channel errors — e.g. Xid `13`,
    `31`, `43`, `45`, and the graphics-exception family. These correlate with an
    undervolt that is too aggressive.
  - Ignore / do not downstep: app-caused faults with no instability signal, and
    informational Xids.
- Optional corroboration: ECC/throttle counters, or a clock/voltage anomaly, to
  raise confidence before acting.

### Reaction ladder (conservative, cooldown-gated)

1. **First qualifying event:** one downstep of the live curve — raise the lock
   voltage by one bin (safer and quicker than dropping clock), apply it live.
   Start a cooldown (~15 s) and allow only one adjustment in flight, so a burst
   of Xids from a single incident cannot cascade.
2. **Repeated events** (≥K within a session/window): drop one tier
   (Performance → Balanced → Efficiency) and apply that tier's curve.
3. **Bounded:** cap the number of downsteps and floor the voltage/clock; after a
   max-consecutive-events threshold, fall to the lowest tier and stop adjusting.

### Persistence (profile-scoped, generic)

- Remember the adjustment against the **active Auto-UV profile** (not per game):
  store `{profile_id, applied_adjustment | safer_tier, event_count, last_seen}`
  in a small JSON, mirroring the shape of `auto_uv/run/crash_recovery.py`.
- On next startup / next apply of that profile, start from the remembered safe
  point instead of the raw scanned one.
- (Optional, later) key by foreground app id if we already track it — but default
  is profile-scoped to honour the no-per-game-logic stance.

### False-positive discipline (mirror the watchdog)

- Only the instability Xid class triggers; a single benign event does nothing.
- Cooldown + single-in-flight adjustment; bounded total downsteps.
- Every action is logged and surfaced to the user (what changed and why).
- Bias: prefer under-reacting (a rare uncaught crash) over degrading a stable
  profile the user tuned.

### Arbitration with the FPS-adaptive controller

Both the existing FPS logic and this guard want to change the live curve.
**Safety wins:** a stability downstep pins a floor the FPS controller may not
raise above until the session ends (or a stable-for-N-minutes window elapses).

---

## Risks / open questions

- **Xid semantics on Linux** differ from Windows event IDs; the severity mapping
  must be validated against real crash logs before it drives any action.
- **Foreground detection under Wayland/gamescope** if we ever go per-app — avoid
  for the default profile-scoped design.
- **Interaction with the adaptive controller** needs a clear priority rule.
- **Distinguishing game bugs from GPU instability** — an app crash is not an
  undervolt failure; the Xid class filter is the main guard, corroboration helps.

---

## Phasing

1. **Observe only.** Poll + classify Xids during gameplay, log them, no action.
   Validate the classification on real crashes. Zero risk.
2. **Single downstep + cooldown.** Act on the first qualifying event, one step,
   heavily gated.
3. **Tier ladder + persistence.** Repeated-event tier drop and profile-scoped
   memory.

Each phase is independently shippable and independently reversible (a config
flag disables the guard entirely, like `hang_watchdog_s=0` disables the scan
watchdog).

## Testing

- Unit-test the Xid **classifier** and the **reaction ladder** as pure functions
  (injected synthetic Xid streams → expected action / no-action).
- Unit-test the persistence read/write and the "start from remembered safe point"
  path.
- A real hang is not integration-testable; rely on injected events, as the
  scan-time watchdog tests do.
