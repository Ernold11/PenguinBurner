# Latency Telemetry Change Set — 2026-06-09

Summary of all changes made to the PenguinBurner Vulkan latency layer and its
receiver in this work session. Goal: capture the real pre-frame-generation GPU
render latency (the number the NVIDIA App overlay shows on Windows) on Linux, as
an adaptive-undervolt control signal — without the stuck-constant values and
crashes the earlier experiments hit.

Context / prior art: [auto-profile-latency-findings.md](./auto-profile-latency-findings.md),
[re9-latency-experiment-findings.md](./re9-latency-experiment-findings.md).
How to debug/verify on a GPU host: [latency-telemetry-debugging.md](./latency-telemetry-debugging.md).

> Status: **not yet validated on an NVIDIA host.** This environment has no NVIDIA
> GPU. Everything below is verified by unit tests, a no-GPU end-to-end socket
> test, and clean builds — but the live RE9/Proton confirmation is still pending
> (deferred by request).

---

## 1. Root causes addressed

| Symptom | Root cause | Fix |
|---------|-----------|-----|
| Latency stuck at a single value (~24.3 ms) | Layer emitted only the **newest** `vkGetLatencyTimingsNV` report each present; when the driver's Reflex ring froze after a transition it re-latched the same frame forever. The 24 ms was `gpuRenderEnd − presentStart` of the last live frame. | **Per-frame emission** (§2) + **stale re-emit handoff** so the receiver flags it instead of freezing. |
| VRAM OOM / hard freeze / `libnvidia-gpucomp` SIGSEGV | The opt-in GPU-timestamp **injection** paths (`unsafe-submit-wrapper`, `unsafe-side-submit`) and frame-ID injection mutated the game's command buffers / submits / present info. Unsafe on the DXVK-NVAPI → VKD3D-Proton → NVIDIA stack. | **Removed entirely** (§4). The layer no longer intercepts `vkQueueSubmit*` at all. |
| No real "GPU processing" number | The metric available (`render_present_us`, frame-to-frame `gpu_frame_time_us`) wasn't the GPU's per-frame render duration. | **`gpu_render_us`** computed from the Reflex report's GPU timestamps (§3). |
| Per-frame emission could re-freeze | Naive `presentID <= last_emitted` dedup breaks when the driver restarts its present-ID counter on swapchain recreation (resolution/HDR/fullscreen change). | **Reset detection** in the emit planner (§5). |

---

## 2. Per-frame emission (the core improvement)

`query_latency_timing()` in `native/latency_layer/src/penguinburner_latency_layer.cpp`
now forwards **every** Reflex frame report whose `presentID` is newer than the
last one already emitted for that swapchain (`SwapchainContext::last_emitted_present_id`),
instead of only the single newest report.

- Genuine per-frame samples → the meter shows live, varying numbers.
- When the ring stops advancing (no fresh reports), the newest report is
  **re-emitted once**. That report carries a duplicate `presentID`, so the C++
  duplicate counter increments and the receiver reports
  `quality=stale-driver-report` with `*-p95=n/a` — an explicit "stream is stale"
  signal rather than a silently frozen value.
- Untagged reports (`presentID == 0`, driver didn't tag them) are always treated
  as fresh and never advance the watermark.

## 3. `gpu_render_us` — real GPU render time

The layer now emits, per report:

```
gpu_render_us = gpuRenderEndTimeUs − gpuRenderStartTimeUs   (when both present and end > start)
```

This is the per-frame, pre-frame-generation GPU processing time — the closest
Linux equivalent of the NVIDIA App overlay's render-latency number, and the
intended adaptive control signal (target ~16.6 ms). Because Reflex markers track
the real input-bearing rendered frames, DLSS3 frame-generation (x2/x3/x4/adaptive)
generated frames do not pollute it.

Plumbed through:
- emitted in the `driver-report` JSON (`send_timing_sample`),
- surfaced as `gpu-render-p95` in the meter summary,
- included in the `event=latency-raw` log allowlist.

Tested test-first on the Python side (`tests/test_latency_telemetry.py`).

## 4. Injection removed (safety)

Deleted both opt-in injection subsystems entirely:

- **GPU-timestamp injection** — env vars `PENGUIN_BURNER_LATENCY_GPU_TIMESTAMPS`
  (`unsafe-submit-wrapper` / `unsafe-side-submit`) and
  `PENGUIN_BURNER_LATENCY_GPU_TIMESTAMP_INTERVAL`; query pools, command-buffer
  recording, side/wrapper submits, slot bookkeeping, ~18 helper functions, 4
  structs, and the related `DeviceContext` members.
- **Frame-ID / present-ID injection** — env var
  `PENGUIN_BURNER_LATENCY_INJECT_FRAME_IDS`; `VkPresentIdKHR` /
  `VkLatencySubmissionPresentIdNV` injection and the `VK_KHR_present_id`
  device-create amendment.
- The layer **no longer hooks `vkQueueSubmit` / `vkQueueSubmit2` /
  `vkQueueSubmit2KHR`** — they were pure injection wrappers. This also removes the
  per-submit global-mutex lock from the hot path.
- Receiver: removed the now-dead `gpu-submit-proxy` quality level/branch and the
  permanently-`n/a` `gpu-submit-p95` column; pruned dead `gpu_timestamp_*` and
  `gpu_submit_us`/`cpu_age_us` fields from the log allowlists.

Net: `penguinburner_latency_layer.cpp` shrank from **2913 → ~1675 lines**
(~1240 removed). The layer is now read-only telemetry: it observes Reflex
markers/timings and never mutates the game's Vulkan work.

These flags are kept only as historical record in
`re9-latency-experiment-findings.md`; they are no longer recognized.

## 5. presentID-reset bug fix + extracted, tested logic

The per-frame dedup decision was extracted into a pure, unit-tested header:

- `native/latency_layer/src/latency_emit_plan.h` — `plan_latency_emits(present_ids, last_emitted)`
  returns which reports to emit, the new watermark, the newest report position,
  and a `reset_detected` flag.
- **Reset detection:** if the driver returns tagged reports whose highest
  `presentID` is *below* the current watermark, the present-ID counter restarted
  (swapchain recreation). The planner resets the watermark to 0 and emits the
  fresh low-numbered frames, instead of dropping them all as "already seen" —
  which would otherwise reintroduce a permanent false-stale state on exactly the
  transitions RE9 hits.
- `native/latency_layer/test/test_latency_emit_plan.cpp` — standalone unit test
  (fresh sequence, stale ring, present-ID reset, untagged reports, empty, newest
  selection). Wired into CMake/CTest; run with `ctest` in the build dir.

`query_latency_timing()` now calls `plan_latency_emits()` rather than inlining the
logic, so the layer and the test cannot drift.

## 6. Present-pacing FPS (no-Reflex fallback)

A bottom-tier cadence signal that works for **any** Vulkan app, with no Reflex,
no markers, no MangoHud, and no game cooperation.

- The present hook (`layer_queue_present_khr`) now measures the present-to-present
  interval per swapchain (`SwapchainContext::last_present_us`) and emits
  `send_present_pacing_sample` → `measurement=present-pacing
  quality=present-frametime present_frametime_us=<delta>`.
- Receiver surfaces `present-frametime-p95` (hitch/tail) and `present-fps` (derived
  from the **median** frametime via `_median_us`/`_format_fps`, so one stutter in
  the p95 tail doesn't crater the rate).
- **It counts *presented* frames**, so with DLSS3 frame generation it reflects
  inflated presentation FPS, not the real rendered-frame cadence. It is tagged at
  the lowest quality tier and must be treated as liveness/pacing, never as
  input-bearing latency. Differentiation rule documented in
  [latency-alternative-sources.md](./latency-alternative-sources.md) §5.

Tested test-first on the receiver side; the layer change builds clean and was
verified end-to-end over the socket on a no-GPU host
(`present-frametime-p95=50.00ms present-fps=60` from synthetic samples).

---

## Files changed

| File | Change |
|------|--------|
| `native/latency_layer/src/penguinburner_latency_layer.cpp` | per-frame emission, `gpu_render_us`, present-pacing FPS, injection removal, uses emit planner |
| `native/latency_layer/src/latency_emit_plan.h` | **new** — pure emit-decision logic + reset detection |
| `native/latency_layer/test/test_latency_emit_plan.cpp` | **new** — unit tests for the planner |
| `native/latency_layer/CMakeLists.txt` | build + register the unit test (CTest) |
| `latency_telemetry/receiver.py` | `gpu-render-p95`, `present-frametime-p95` + `present-fps` metrics; removed dead `gpu-submit-proxy` / timestamp fields |
| `tests/test_latency_telemetry.py` | `gpu_render_us` + present-pacing tests; removed `gpu-submit-proxy` test |
| `docs/re9-latency-experiment-findings.md` | update noting injection removal + the read-only fixes; link to debug guide |
| `docs/latency-telemetry-debugging.md` | **new** — GPU-host debug/verify guide |
| `docs/latency-alternative-sources.md` | **new** — non-Reflex fallback sources + Reflex/non-Reflex differentiation rule |
| `docs/latency-telemetry-changes-2026-06-09.md` | **new** — this document |

## Verification done (no GPU)

- `ctest` in the layer build dir: `latency_emit_plan` passes (6 cases incl. the
  reset regression).
- Layer builds clean with `-Wall -Wextra -Wpedantic -fvisibility=hidden`, zero
  warnings; shared library links.
- Python suite: full run green; `tests/test_latency_telemetry.py` 13 passed
  (incl. present-pacing).
- End-to-end socket test on a no-GPU host: 3 advancing-`present_id` synthetic
  samples produced 3 per-frame raw lines and `gpu-render-p95=15.90ms` in the
  meter; a duplicate-count sample produced `quality=stale-driver-report ...
  gpu-render-p95=n/a`; present-pacing-only samples produced
  `present-frametime-p95=50.00ms present-fps=60`.

## Still pending

- **Live validation on an NVIDIA + Proton host** (RE9, Steam `3764200`) — confirm
  `gpu_render_start/end_us` are populated through DXVK-NVAPI/VKD3D-Proton, that
  `gpu_render_us` tracks load and isn't frozen, and that the stale handoff fires
  on menu / DLSS-FG-mode transitions. Steps: see
  [latency-telemetry-debugging.md](./latency-telemetry-debugging.md) §5.
- **No consumer yet.** The meter only logs; nothing reads `gpu_render_us` to
  switch efficiency/balanced/performance profiles. A test run can validate the
  *number*, not *adaptation*. Building the control loop (target 16.6 ms,
  promote-fast/demote-slow, cooldown, confidence gating, hold-on-stale) is the
  next feature — see policy sketch in
  [auto-profile-latency-findings.md](./auto-profile-latency-findings.md).
