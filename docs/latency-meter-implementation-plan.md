# Latency Meter Implementation Plan

Date: 2026-06-10

Design: [docs/superpowers/specs/2026-06-10-latency-meter-design.md](./superpowers/specs/2026-06-10-latency-meter-design.md)

Feature: compute system latency in the PenguinBurner layer from Reflex marker
CPU timestamps (`sim_to_present_us`, `submit_to_present_us`, OOB variants),
independent of the stall-prone `vkGetLatencyTimingsNV` report ring.

## Phase 0 — Validate anchors from existing captures (no code)

The plan rests on two facts that the existing `latency-marker-coverage` events
can confirm from captures already on disk:

```bash
ls ~/.cache/penguin-burner/latency-captures/

# 1. Did simulation_start / present_end keep advancing through the stale interval?
rg -o 'latency-marker-coverage.*' <capture>.log \
  | rg -o '"simulation_start":[0-9]+|"present_end":[0-9]+|"out_of_band_present_end":[0-9]+' \
  | sort | uniq -c | tail -40

# 2. Around the stall point (~present_id 470): do in-band present markers stop
#    while OOB present markers start?
rg 'latency-stream-stale|out_of_band_present' <capture>.log | head -50
```

Decision table:

| Observation | Consequence |
|-------------|-------------|
| `simulation_start` advances throughout | `sim_to_present_us` tier is viable as planned |
| `simulation_start` absent/stops | headline tier falls back to `submit_to_present_us`; overlay label stays "latency (est.)" |
| in-band `present_end` stops at stall, OOB advances | OOB spans are mandatory (Phase 1 step 3), and the ring-stall root cause points at OOB present correlation |
| both present marker families stop at stall | marker meter dies with the ring; feature is not viable for RE9 — stop and reassess |

### Phase 0 Results (2026-06-10)

Checked against `~/.cache/penguin-burner/latency-captures/re9-lag-select-live-20260609-2244.log`
(135 s live RE9 session, `present-flow` snapshots):

- **Simulation markers exist and never stall.** `last_simulation_present_id`
  advances 4 → 11304 across the full session, in lockstep with rendersubmit
  and present markers. The `sim_to_present_us` headline tier is viable.
- **In-band present markers survive the whole run** (`last_present_marker_present_id`
  reaches 11303) and **OOB present markers advance at the same cadence**
  (11302) — both span variants have data, and the OOB family fires at
  base-frame cadence (consistent with `oob-present-start` being the top
  `BASE_FRAME_MARKER_PRIORITY` entry).
- Oddity to keep an eye on: `last_oob_render_submit_present_id` froze at 5905
  mid-run while every other marker family kept advancing.
- **`last_vulkan_present_id=0` for the entire run** — vkd3d attaches no
  `VkPresentIdKHR`, so present-wait correlation is unavailable and marker↔real-
  present matching must be done by time, not ID.
- **`last_driver_report_present_id=0` for the entire run** — in the current
  launch configuration the driver timing ring produces *nothing*, so Phase 3
  (calibration against fresh driver reports) has no calibration source.
  **Phase 3 is cut**, not deferred (see below).
- The capture's `marker-proxy` lines predate the `sim_us`/`present_marker_us`
  fields, so the `PRESENT_END` semantics question (presented vs enqueued)
  remains open. PR 1 ships a diagnostic field to settle it from one live run
  (Phase 1 step 5).

Verdict: green light for PR 1 with the open question instrumented rather than
assumed.

## Phase 1 — Layer: compute and emit cross-phase spans (~80 lines)

File: `native/latency_layer/src/penguinburner_latency_layer.cpp`

1. **Spans.** In `send_marker_timing_sample`, add:
   - `sim_to_present_us = elapsed_us(timing.sim_start_us, timing.present_end_us)`
   - `submit_to_present_us = elapsed_us(timing.render_submit_start_us, timing.present_end_us)`
   - `sim_to_oob_present_us = elapsed_us(timing.sim_start_us, timing.oob_present_end_us)`

   Append all three to the `marker-proxy` JSON line (keep zeros when a span is
   incomplete, matching the existing fields' behavior).

2. **Emit trigger.** Extend `marker_timing_metric_bits` with new bits so the
   emit-on-new-metric path in `observe_latency_marker` fires when
   `PRESENT_END` (or OOB present end) arrives, even for frames that complete no
   intra-phase pair:
   - bit 4 = `sim_start→present_end`
   - bit 5 = `render_submit_start→present_end`
   - bit 6 = `sim_start→oob_present_end`

   Keep the existing "emit once per new metric bits" dedup — a frame will
   typically emit once at `RENDERSUBMIT_END` and once at `PRESENT_END`; the
   receiver keeps the newest.

3. **OOB timestamps.** `MarkerTiming` gains `oob_present_start_us` /
   `oob_present_end_us` (and, for symmetry/diagnostics,
   `oob_render_submit_start_us/end_us`). The OOB cases in
   `observe_latency_marker` currently only bump counters; store the
   `marker_time_us` already captured at the top of the function.

4. **Quality.** Extend `marker_timing_quality` ordering:
   `input→present` > `sim→present` > `sim→oob-present` > `submit→present` >
   existing intra-phase fallbacks.

5. **`PRESENT_END` semantics diagnostic (answers the open Phase 0 question).**
   `SwapchainContext.last_present_us` already records the layer's own
   `vkQueuePresentKHR` time. On a `PRESENT_END` marker, also emit
   `present_marker_lag_us = marker_time_us − swapchain.last_present_us`.
   One live run then settles the semantics:
   - tight, small, mostly-positive distribution → the marker fires at the real
     present and the spans are honest;
   - wide or negative-heavy distribution → the marker is enqueue-time, and the
     span's end anchor must switch to the layer's own present timestamp
     (nearest-in-time association, since `last_vulkan_present_id=0` rules out
     ID matching).
   The fix path either way lives in the same code, so no second PR is needed.

Constraints (hold the safety class):

- No new locks — everything happens under the existing `g_mutex` section in
  `observe_latency_marker`.
- No per-frame allocation — new fields are POD members of `MarkerTiming`.
- No Vulkan calls added; pure observation.

## Phase 2 — Receiver: tiers, aggregation, meter, snapshot (~120 lines)

File: `latency_telemetry/receiver.py`

1. **Parsing.** `normalize_timing_sample`: pass through the three new fields;
   derive nothing (the layer ships final spans).
2. **Quality ladder.** Bump `reflex-marker-input-present` from 3 to 4, then add
   `reflex-marker-sim-present: 3` and `reflex-marker-submit-present: 3`. (An
   input-sample anchor is strictly earlier and more complete than a sim-start
   anchor, so the input tier must outrank the sim tier — the original draft of
   this plan had that inverted.) Extend `_quality_for_sample` for marker-proxy
   samples carrying the new spans, ranked below `input_to_present_us`.
3. **Aggregation.** Extend `_latency_proxy_p95` preference order:
   real `input_to_present` → marker `input_to_present` → `sim_to_present` →
   `sim_to_oob_present` → `submit_to_present`. Track *which* tier produced the
   p95 so the snapshot can publish it.
4. **Meter line.** Add `sim-to-present-p95` and `submit-to-present-p95`
   columns. Note: `summary()` currently builds the cadence-field tail twice
   (early-return branch and full branch) — extract the shared tail into one
   helper *before* adding columns, or the two copies will drift (flagged in the
   2026-06-10 review).
5. **Snapshot.** Publish `latency_p95_ms` (best tier's value, ms) and
   `latency_quality` (tier name). Keep `None` when no tier is live — consumers
   already handle absent metrics.
6. **Marker-vocabulary constants.** The marker-name/quality strings are now
   shared C++↔Python protocol in three places; collect the Python side into
   module-level constants next to `BASE_FRAME_MARKER_PRIORITY` with a comment
   naming the C++ counterpart functions (`marker_timing_quality`,
   `latency_marker_name`).

## Phase 3 — Calibration against the fresh driver ring — **CUT (2026-06-10)**

Phase 0 showed `last_driver_report_present_id=0` for the entire current-config
RE9 run: the driver timing ring produces nothing to calibrate against on this
stack, and earlier findings showed that even fresh reports carried
`gpu_render_start/end_us=0` and no usable sim→present span. There is no
calibration source, so this phase is removed rather than deferred. If a future
driver/config restores a live ring, the original design (span-to-span offset,
EWMA, `reflex-marker-calibrated` tier) is recorded in the
[design doc](./superpowers/specs/2026-06-10-latency-meter-design.md) for
revival.

## Phase 4 — Consumers (separate PR)

- `runtime_gpu_control/adaptive_profile_runtime.py`: accept
  `submit_to_present_p95` as an additional pressure input, gated on
  `latency_quality`; hysteresis identical to the present-fps path.
- `penguin_burner_overlay/`: show `sim-to-present` (or calibrated) as
  `XX ms` with quality badge per the overlay design doc.
- Update [latency-alternative-sources.md](./latency-alternative-sources.md)
  §5's quality ladder and meter columns.

## Tests

- **C++**: restore a native test target (the emit-plan test was removed):
  `native/latency_layer/test/test_marker_timing.cpp` covering
  `marker_timing_metric_bits` (new bits fire on present-end arrival, partial
  frames emit nothing, OOB spans), `marker_timing_quality` ordering, and
  re-emit dedup via `emitted_metric_bits`.
- **Python** (`tests/test_latency_telemetry.py`): tier selection with and
  without `INPUT_SAMPLE`; p95 preference order including OOB fallback;
  meter line shape with the new columns in both summary branches; snapshot
  `latency_p95_ms`/`latency_quality` including the `None` case.

## Live validation (RE9)

1. Run with the standard launch line; confirm `sim-to-present-p95` stays live
   past the ~470-present stall point while `gpu-render-p95` goes `n/a`.
2. During the fresh ring window, log marker span vs driver span side by side —
   expect a stable offset (this is also the Phase 3 feasibility check).
3. A/B frame generation off/on in the same session:
   - quantifies the FG hold-back bias on `sim_to_present`;
   - doubles as the ring-stall root-cause experiment (if the ring never stalls
     with FG off, the stall is FG present-path correlation, not swapchain
     recreation per se).
4. Sanity magnitudes: at ~54 base FPS with Reflex active, `sim_to_present_p95`
   should land roughly in the 30–60 ms band; `submit_to_present_p95` strictly
   below it; both must rise visibly when forcing the lowest undervolt tier.

## Risks

| Risk | Mitigation |
|------|------------|
| RE9 emits no simulation markers | Phase 0 detects; `submit_to_present` remains the control signal |
| In-band present markers vanish under FG | OOB spans (Phase 1 step 3) |
| Marker stream itself dies mid-session | 3 s window ages out → natural degrade to present-pacing tier; no frozen values |
| Double emit per frame inflates sample counts | receiver dedups by `present_id`, newest wins (existing behavior) |
| presentID reset on swapchain recreation | bounded 256-entry `marker_order` ring already evicts; spans never cross IDs |

## Sequencing

- PR 1: Phase 1 + Phase 2 + tests (the meter itself; ~1 day). Includes the
  `present_marker_lag_us` diagnostic; the first live RE9 run after PR 1 closes
  the `PRESENT_END` semantics question and validates magnitudes (30–60 ms band
  expected at ~54 base FPS).
- PR 2: Phase 4 consumers (controller + overlay). Controller wiring is
  conditional: only key profile decisions off `submit_to_present_p95` if live
  captures show it diverging from `base_present_frametime_p95_ms` (the
  "same FPS, worse latency" queue regime); otherwise it ships as
  overlay/diagnostic only.
- Phase 0 is complete (results above); Phase 3 is cut.
