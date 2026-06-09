# Alternative GPU-Latency Sources on Linux

If `VK_NV_low_latency2` (Reflex) does not yield a usable per-frame GPU number on a
given title/driver — e.g. driver 610 leaves `gpuRenderStartTimeUs` /
`gpuRenderEndTimeUs` at 0, or the Reflex stream goes stale — these are the
Reflex-independent fallbacks. **None of them mutate the game's Vulkan work**, so
they cannot reproduce the VRAM OOM / freeze / `libnvidia-gpucomp` crashes that the
deleted timestamp-injection paths caused (see
[re9-latency-experiment-findings.md](./re9-latency-experiment-findings.md)).

Context: the NVIDIA App overlay's "Render Latency" is itself just Reflex
(`NvAPI_D3D_GetLatency` → the same per-frame report struct as
`vkGetLatencyTimingsNV`). There is no hidden richer API behind it. What Windows
has that Linux lacks is **ETW / PresentMon** — kernel GPU-scheduler events the OS
emits without game cooperation. The closest Linux equivalents are the two DRM
mechanisms in §1.

Quality ladder (highest fidelity → conservative floor):

```
Reflex gpu_render_us  →  Reflex input_to_present_us  →  drm_fdinfo engine-busy
   (per-frame GPU work)     (full end-to-end, CPU markers)   (per-process GPU busy)
        →  dma-fence tracepoints (per-submit GPU latency)
        →  present-pacing FPS (this layer, any Vulkan app)
        →  NVML pressure (coarse)
```

A consumer/control loop should target this interface, not a single source, so the
GPU test run only decides *which tier this stack lands on* — it works at some tier
either way. See §5 for the critical rule on **not comparing latency-tier and
cadence-tier numbers directly.**

---

## 1. DRM scheduler signals (the Linux "PresentMon-like" layer)

The Linux kernel's DRM subsystem (GPU scheduler + `dma-fence`) is where
GPU-execution timing lives below any single API. Two ways to read it, both
out-of-process and injection-free.

> **Hard unknown (test it):** how much of this NVIDIA's proprietary / open kernel
> modules actually populate on driver 610 is version-specific. The AMD/Intel
> (amdgpu/i915/xe) drivers fill these richly; NVIDIA has historically lagged.
> Confirm on the GPU host before building on it.

### 1a. `drm_fdinfo` — per-process GPU engine busy time

Each process holding a DRM file descriptor exposes cumulative per-engine busy
counters in `/proc/<pid>/fdinfo/<fd>`. This is what `nvtop` / `gputop` read.

```bash
# Find the game's DRM fdinfo and look for engine-busy fields:
for fd in /proc/$(pgrep -n -f re9.exe)/fdinfo/*; do
  grep -lE 'drm-(engine|cycles|maxfreq)' "$fd" 2>/dev/null
done

# Inspect one:
cat /proc/$(pgrep -n -f re9.exe)/fdinfo/<fd>
```

Expected fields (when supported):

```text
drm-driver:        nvidia            # or nvidia-drm
drm-engine-gfx:    123456789 ns      # cumulative GPU graphics busy nanoseconds
drm-engine-compute: ...
drm-cycles-gfx / drm-maxfreq-gfx     # some drivers expose cycles+freq instead
```

**How to turn it into a latency-ish signal:** sample `drm-engine-gfx` at two times
`t0`, `t1`; `busy_fraction = (busy(t1) - busy(t0)) / (t1 - t0)`. Near 1.0 over a
frame interval ⇒ GPU is the bottleneck. It is **per-process** (attributes load to
the game, unlike NVML which is whole-GPU), and needs **no layer at all**.

- Pro: process-attributed, zero injection, trivial to read.
- Con: it's *busy time*, not per-frame render latency or input-to-photon. Tells
  you "GPU saturated by this game," not "frame N took X ms."
- **610 caveat:** NVIDIA may expose only a subset (or none) of `drm-engine-*`.
  If `drm-driver: nvidia` appears but no `drm-engine-*` lines, this source is
  unavailable on the stack — fall through to NVML (§2).

### 1b. `dma-fence` / `gpu_scheduler` tracepoints — per-submit GPU latency

This is the **safe version of the timestamp injection that was removed**. Instead
of writing `vkCmdWriteTimestamp` into the game's command stream (which crashed),
passively observe when each GPU submission's fence is *submitted* vs *signaled*
from outside the process. The delta is that submission's GPU execution latency.

Relevant kernel tracepoints (availability is driver-dependent):

```text
dma_fence:dma_fence_init
dma_fence:dma_fence_emit          # fence created / work submitted
dma_fence:dma_fence_signaled      # GPU finished that work
gpu_scheduler:drm_sched_job       # job queued to the scheduler
gpu_scheduler:drm_run_job         # job started on the GPU
gpu_scheduler:drm_sched_process_job
```

Check what this host actually has:

```bash
sudo ls /sys/kernel/tracing/events/dma_fence/
sudo ls /sys/kernel/tracing/events/gpu_scheduler/   # may be absent on NVIDIA
```

Two ways to consume:

**ftrace (quick look):**
```bash
cd /sys/kernel/tracing
echo 1 | sudo tee events/dma_fence/enable >/dev/null
sudo cat trace_pipe        # watch fence emit/signaled timestamps; Ctrl-C to stop
echo 0 | sudo tee events/dma_fence/enable >/dev/null
```

**eBPF (production):** attach to `dma_fence_emit` / `dma_fence_signaled`, key by
fence context+seqno, record `signaled_ts - emit_ts` per submission, aggregate p95
per second. (`bpftrace` prototype, then a small loader if it pans out.)

- Pro: real per-submit GPU latency, out-of-process, no game cooperation, no
  mutation. The legitimate way to get what the injected timestamps were after.
- Con: needs root / `CAP_SYS_ADMIN` (or `CAP_BPF` + `CAP_PERFMON`) for tracing;
  correlating fences back to *presented frames* is non-trivial (you get submit
  latency, not necessarily whole-frame or input-to-photon).
- **610 caveat:** the proprietary NVIDIA driver may not register the common
  `gpu_scheduler` tracepoints and may use its own fence implementation, so
  `dma_fence` events could be sparse or absent. `gpu_scheduler/` is most likely
  to be missing. **Verify with the `ls` above before investing in eBPF.**

---

## 2. NVML pressure proxy (always-available conservative floor)

NVML is whole-GPU, sampled (not per-frame), and Reflex-independent. It can't see
frame generation or measure latency, but it reliably answers *"is the GPU the
bottleneck and is it being held back?"* — enough for a conservative
efficiency→performance promotion. **PenguinBurner already has the throttle-reason
reader**, so this is the cheapest fallback to wire up.

### Already in the repo

`nvml_perf_cap_reason.py` → `NvmlPerfCapReasonReader` reads NVML's current clock
throttle-reason bitmask via `nvmlDeviceGetCurrentClocksThrottleReasons` /
`...EventReasons`:

```python
from nvml_perf_cap_reason import NvmlPerfCapReasonReader

reader = NvmlPerfCapReasonReader(gpu_index=0)
mask = reader.read_mask()       # raw bitmask, or None if unavailable
label = reader.read_reason()    # e.g. "sw-power,hw-thermal"
reader.close()
```

Decoded reason labels (`PERF_CAP_REASON_BITS` in that file):

| Bit | Label | Meaning for the policy |
|-----|-------|------------------------|
| `GPU_IDLE` | `idle` | GPU not the bottleneck → don't promote |
| `APPLICATIONS_CLOCKS_SETTING` | `app-clocks` | clocks pinned by app/config |
| `SW_POWER_CAP` | `sw-power` | **power-limited → headroom exists, promotion may help** |
| `HW_SLOWDOWN` | `hw-slowdown` | hardware protection engaged |
| `SW_THERMAL_SLOWDOWN` | `sw-thermal` | **thermal-limited → promotion won't help; demote/hold** |
| `HW_THERMAL_SLOWDOWN` | `hw-thermal` | hard thermal cap |
| `HW_POWER_BRAKE_SLOWDOWN` | `hw-power-brake` | external power-brake |
| `SYNC_BOOST` / `DISPLAY_CLOCK_SETTING` | `sync-boost` / `display-clock` | informational |

### Combine throttle reasons with utilization + clock headroom

The throttle mask alone isn't enough; pair it with two more NVML reads (not yet
bound in `nvml_gpu_policy.py` — would need adding via the same `hasattr`/`argtypes`
pattern used there):

- `nvmlDeviceGetUtilizationRates` → `.gpu` (% SM busy). Pegged ~100% = GPU-bound.
- `nvmlDeviceGetClockInfo(GRAPHICS)` vs `nvmlDeviceGetMaxClockInfo(GRAPHICS)` →
  current vs achievable clock; a gap while GPU-bound = throttled, not maxed.
- Optionally `nvmlDeviceGetProcessUtilization` to attribute to the game PID.

### Heuristic the controller can use

```text
gpu_pressure = HIGH   when  gpu_util >= ~95%  AND  throttle ∈ {sw-power, hw-*}
                            (GPU saturated AND being held back)
             = THERMAL when  thermal slowdown bits set
                            (do NOT promote — more clock = more heat; hold/demote)
             = LOW     when  idle, or util well below 100%
                            (GPU not the bottleneck)
```

- Pro: always available, no layer, no root, process-agnostic; the existing reader
  already covers the throttle half.
- Con: coarse (poll-rate sampled, whole-GPU), blind to frame generation, "GPU
  busy" ≠ "frame budget missed." Use as the floor, never the only signal.

---

## 2.5. Present-pacing FPS (this layer, no Reflex, no MangoHud)

**Implemented.** The layer already hooks `vkQueuePresentKHR`, so it measures the
present-to-present interval and emits it directly — no Reflex markers, no MangoHud,
no game cooperation, works for **any** Vulkan app.

- Emitted sample (`send_present_pacing_sample`):
  `measurement=present-pacing quality=present-frametime present_frametime_us=<delta>`.
- Receiver surfaces `present-frametime-p95` (the hitch/tail) and `present-fps`
  (derived from the **median** frametime, so a single stutter in the p95 tail does
  not crater the reported rate).
- This is the bottom telemetry tier — it always works as long as the layer loads,
  and it keeps producing data when the Reflex stream goes stale (the RE9 case).

**Critical caveat — it counts *presented* frames.** With DLSS3 frame generation
active, generated frames are presented too, so `present-fps` is the *inflated*
presentation rate, **not** the real rendered-frame cadence. This is precisely the
"huge FPS, unplayable latency" blind spot the project exists to avoid. Treat
`present-fps` as **liveness / pacing**, never as input-bearing latency. See §5.

- Pro: zero game cooperation, zero injection, universal, cheap; feeds the
  CPU/GPU-bound verdict (frametime) and is a reliable liveness signal.
- Con: presented-FPS (frame-gen-blind); cadence, not latency.

---

## 3. Decision after the GPU test run

```text
Reflex gpu_render_start/end_us nonzero?
  ├─ yes → use gpu_render_us (current headline metric). Done.
  └─ no  → Reflex CPU markers present (render_submit_us / input_to_present_us)?
            ├─ yes → use input_to_present_us (full Reflex latency; closest to the
            │        overlay number anyway). One-line fallback, no new source.
            └─ no  → game/driver Reflex unusable → drop to a non-Reflex source:
                      drm_fdinfo engine-busy   (if drm-engine-* present)   §1a
                      dma-fence tracepoints    (if events present, want per-submit) §1b
                      present-pacing FPS       (always; cadence not latency) §2.5
                      NVML pressure            (always; conservative floor) §2
```

Quick triage commands on the GPU host:

```bash
# Reflex GPU fields populated?
journalctl -u PenguinBurner.service -o cat | grep -oE 'gpu_render_(start|end)_us=[0-9]+' | sort -u | head

# drm_fdinfo engine-busy available?
cat /proc/$(pgrep -n -f re9.exe)/fdinfo/* 2>/dev/null | grep -E 'drm-driver|drm-engine'

# DRM tracepoints available?
sudo ls /sys/kernel/tracing/events/dma_fence/ /sys/kernel/tracing/events/gpu_scheduler/ 2>&1

# NVML throttle reasons (uses the repo reader):
python -c "from nvml_perf_cap_reason import NvmlPerfCapReasonReader as R; r=R(0); print('throttle:', r.read_reason()); r.close()"
```

## 4. Open unknowns (post-cutoff / hardware-specific)

These need the GPU host or current research to answer; I can't confirm them from
this environment:

- Does driver 610 populate `gpuRenderStart/EndTimeUs` through DXVK-NVAPI →
  VKD3D-Proton, or are they always 0? (Determines if §3's first branch is viable.)
- Does NVIDIA's Linux driver expose `drm-engine-*` in `drm_fdinfo` on 610?
- Does it register `dma_fence` / `gpu_scheduler` tracepoints, or use a private
  fence path that's invisible to ftrace/eBPF?

## 5. Differentiating Reflex vs non-Reflex metrics

Every sample carries a `quality=` tag, and the meter reports the highest-ranked
quality present plus each metric in its **own named column**, so latency and
cadence numbers are never collapsed into one figure. The ladder (low → high):

| `quality=` | Source | Reflex? | Measures | Frame-gen aware? |
|------------|--------|---------|----------|------------------|
| `present-frametime` | present pacing (§2.5) | ❌ | **presented** cadence | **no** |
| `driver-timing` | driver timestamps | ✅ (driver) | GPU/driver timing, no markers | yes |
| `reflex-marker-*` | marker proxy | ✅ | markers seen | yes |
| `reflex-markers` | reflex markers | ✅ | real-frame timing, partial | yes |
| `reflex-render-submit` | render-submit markers | ✅ | real-frame render submit | yes |
| `reflex-input-present` | `input_to_present_us` | ✅ | full end-to-end real-frame latency | yes |
| `stale-driver-report` | — | — | Reflex went stale; meter holds, reports `n/a` | — |

Latency-tier columns (Reflex): `gpu-render-p95`, `input-present-p95`,
`render-submit-p95`, `render-present-p95`, `gpu-frame-p95`, `latency-proxy-p95`.
Cadence-tier columns (non-Reflex): `present-frametime-p95`, `present-fps`.

### The rule a consumer MUST follow

**Never compare a cadence-tier number with a latency-tier number, and never let
the cadence tier override a Reflex verdict.** They measure different things:

- Reflex `gpu_render_us` / `input_to_present_us` = **real rendered-frame** latency
  (frame-generation aware — generated frames carry no markers).
- `present-fps` = **presented-frame** rate (frame-generation blind — counts
  generated frames).

If a controller demoted the GPU profile because `present-fps` looked high while
DLSS3 frame generation was inflating it, it would make exactly the wrong call —
the original problem this project set out to avoid. The quality ladder already
encodes precedence (any `reflex-*` outranks `present-frametime`); the policy must
honor it: use Reflex latency when available, and treat `present-fps` as
**liveness / pacing / CPU-vs-GPU input**, not as latency.

### Bonus: present-fps as a frame-generation detector

When both tiers are live, a large divergence is itself a useful signal: if
`present-fps` is ~2–3× the implied real-frame rate from the Reflex stream, frame
generation (x2/x3/x4) is active. That divergence can be logged as an explicit
"frame-gen active" flag rather than discarded as noise.

## References

- Reflex / `VK_NV_low_latency2`: see
  [auto-profile-latency-findings.md](./auto-profile-latency-findings.md),
  [latency-telemetry-changes-2026-06-09.md](./latency-telemetry-changes-2026-06-09.md)
- Debug/verify the current Reflex path:
  [latency-telemetry-debugging.md](./latency-telemetry-debugging.md)
- Existing NVML code: `nvml_perf_cap_reason.py`, `nvml_gpu_policy.py`
- `drm_fdinfo` spec: kernel `Documentation/gpu/drm-usage-stats.rst`
- DRM scheduler / dma-fence tracepoints: kernel
  `/sys/kernel/tracing/events/{dma_fence,gpu_scheduler}/`
