# Latency Telemetry Debugging Guide

Audience: an agent (or human) with access to an NVIDIA + Proton host who needs to
verify or debug the PenguinBurner Vulkan latency layer — especially the two things
the 2026-06-09 change set touched:

1. **per-frame emission** (no more latched constant latency), and
2. **`gpu_render_us`** — the real pre-frame-generation GPU render time.

> The live RE9 runs on 2026-06-09 validated the plumbing but not the desired
> sustained metric: `gpu_render_us` appears while the Reflex timing ring is
> fresh, then RE9 repeatedly stalls the ring after gameplay/swapchain
> transitions. Treat `quality=stale-driver-report ... gpu-render-p95=n/a` as a
> correct stale-signal handoff, not as a usable latency value.

---

## 0. How telemetry flows (so you know where to look)

```
game process (Proton)                      PenguinBurner daemon
  └─ VK_LAYER_PENGUINBURNER_latency           └─ LatencyTelemetryLogger (latency_telemetry/receiver.py)
       hooks vkQueuePresentKHR                      binds the same Unix DGRAM socket(s)
       calls vkGetLatencyTimingsNV                  parses JSON samples
       sends JSON datagrams  ───────────────►       emits text log lines via runtime_debug.log
                                                          └─ print(flush=True) → stdout
                                                               └─ journald (SyslogIdentifier=PenguinBurner)
```

- The layer **only reads** Reflex timings now. It does **not** intercept
  `vkQueueSubmit*` and performs **no** command-buffer or present-info mutation.
  (The old `unsafe-submit-wrapper` / `unsafe-side-submit` / `INJECT_FRAME_IDS`
  paths that caused the VRAM OOMs, freezes, and `libnvidia-gpucomp` SIGSEGV were
  removed — see `re9-latency-experiment-findings.md`.)
- Log sink: `cli/normal_runtime.py` passes `runtime_debug.log` to the logger;
  `runtime_debug.log` is `print(..., flush=True)` → daemon stdout. Under the
  systemd unit (`PenguinBurner.service`: `StandardOutput=journal`,
  `SyslogIdentifier=PenguinBurner`) that lands in journald.

---

## 1. Build and install the layer

```bash
cmake -S native/latency_layer -B native/latency_layer/build -DCMAKE_BUILD_TYPE=Release
cmake --build native/latency_layer/build
# Produces: native/latency_layer/build/libVkLayer_penguinburner_latency.so
#       and native/latency_layer/build/VkLayer_PENGUINBURNER_latency.json
```

For ad-hoc debugging you do **not** need to system-install. Point the Vulkan
loader at the build dir with `VK_ADD_IMPLICIT_LAYER_PATH`.

### Verify the loader can see the layer (no game needed)

```bash
# Built-in CLI check (wraps vulkaninfo --summary with the enable env set):
python penguin_burner.py --check-latency-layer

# Or directly:
VK_ADD_IMPLICIT_LAYER_PATH=$PWD/native/latency_layer/build \
PENGUIN_BURNER_LATENCY_LAYER=1 \
vulkaninfo --summary 2>&1 | grep -i PENGUINBURNER
```

The layer is an **implicit** layer gated by `enable_environment` in the manifest:
it activates **only** when `PENGUIN_BURNER_LATENCY_LAYER=1` is set. So unrelated
Vulkan apps are never touched unless you opt in per launch.

---

## 2. Launch a Reflex-capable game with telemetry on

Steam launch options for a DX12/Proton title (e.g. RE9, Steam app `3764200`):

```text
VK_ADD_IMPLICIT_LAYER_PATH=/home/jp/git/PenguinBurner/native/latency_layer/build
PENGUIN_BURNER_LATENCY_LAYER=1
PROTON_ENABLE_NVAPI=1
PROTON_HIDE_NVIDIA_GPU=0
DXVK_NVAPI_VKREFLEX=1
PENGUIN_BURNER_LATENCY_RAW_TIMING_LOG=all
gamemoderun %command%
```

- `DXVK_NVAPI_VKREFLEX=1` + `PROTON_ENABLE_NVAPI=1` are what expose
  `VK_NV_low_latency2` through DXVK-NVAPI → VKD3D-Proton.
- **Do NOT** set any `PENGUIN_BURNER_LATENCY_GPU_TIMESTAMPS=...` or
  `PENGUIN_BURNER_LATENCY_INJECT_FRAME_IDS=...` — those flags no longer exist and
  were the cause of the crashes. If you find them in old launch options, delete
  them.

### The raw-timing verbosity knob (`PENGUIN_BURNER_LATENCY_RAW_TIMING_LOG`)

Read by `latency_telemetry/receiver.py` (`_raw_timing_log_interval`):

| Value | Behavior |
|-------|----------|
| unset | log one raw sample per **1.0 s** (default) |
| `all` / `always` | log **every** sample (interval 0.0) — use this for per-frame debugging |
| a number, e.g. `0.25` | one raw sample at most every N seconds |
| `0` / `false` / `no` / `off` | raw per-sample logging **disabled** (meter summary still logs) |

> This env var is read by the **daemon/receiver**, not the layer. Set it in the
> environment the PenguinBurner daemon runs in (or export it before a manual
> foreground run). For a systemd-managed daemon, add it to the unit or run the
> daemon in the foreground (next section).

---

## 3. Where the socket is and how to run the receiver in the foreground

Socket path resolution (`latency_telemetry/receiver.py::latency_socket_paths`),
in order:

1. `$PENGUIN_BURNER_LATENCY_SOCKET` if set (explicit path).
2. else `$XDG_RUNTIME_DIR/penguin-burner/latency.sock`
   (typically `/run/user/1000/penguin-burner/latency.sock`).
3. root/sudo fallbacks via `SUDO_UID` / `SUDO_USER`.
4. **plus** a home-visible fallback `~/.cache/penguin-burner/latency.sock`.

The logger **binds all resolved paths**, so the in-game layer and a root daemon
can rendezvous even across the user/root boundary. The layer picks its send path
with the same env precedence (`PENGUIN_BURNER_LATENCY_SOCKET`, then
`XDG_RUNTIME_DIR`, then `$HOME/.cache/...`).

### Fastest debug loop: run the receiver standalone, print to your terminal

```bash
PENGUIN_BURNER_LATENCY_RAW_TIMING_LOG=all python - <<'PY'
import time
from latency_telemetry import start_latency_telemetry_logger
logger = start_latency_telemetry_logger(log=print)   # prints every line to stdout
print("listening on:", [str(p) for p in logger.paths])
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    logger.close()
PY
```

Launch the game (step 2) **with the matching socket env** so it sends to the same
path this listener bound — e.g. add to the Steam launch options:

```text
PENGUIN_BURNER_LATENCY_SOCKET=/run/user/1000/penguin-burner/latency.sock
```

(Or rely on the `$XDG_RUNTIME_DIR` default if both processes share it.)

### If the daemon owns the socket: read its journal instead

```bash
# live tail
journalctl -u PenguinBurner.service -f -o cat | grep -E 'event=latency-'

# a past window
journalctl -u PenguinBurner.service --since '10 min ago' -o cat | grep -E 'event=latency-'
```

---

## 4. What the log lines look like and which fields matter

Three line types are emitted (formatters in `receiver.py`):

### `event=latency-raw` — one per frame report (gated by RAW_TIMING_LOG)

```text
2026-06-09 21:00:00 event=latency-raw pid=12345 measurement=driver-report \
  device=0x... swapchain=0x... present_id=842 quality=reflex-input-present \
  sample_count=840 timing_count=64 driver_report_count=840 \
  driver_report_duplicate_count=0 marker_bits=127 \
  render_submit_us=2100 render_present_us=12800 input_to_present_us=31000 \
  gpu_frame_time_us=16670 gpu_render_us=14200 input_sample_us=... \
  ... gpu_render_start_us=... gpu_render_end_us=...
```

### `event=latency-meter` — rolled-up summary (every ~10 s)

```text
... event=latency-meter pid=12345 quality=reflex-input-present samples=120 \
  latency-proxy-p95=31.00ms render-submit-p95=2.10ms render-present-p95=12.80ms \
  gpu-render-p95=14.20ms input-present-p95=31.00ms gpu-frame-p95=16.67ms
```

> The exact column set above was reproduced on a no-GPU host by feeding three
> synthetic per-frame samples (`present_id` 841/842/843, `gpu_render_us`
> 13800/14200/15900) into the receiver socket; the meter emitted
> `samples=3 ... gpu-render-p95=15.90ms`, confirming `gpu_render_us` flows from
> datagram → meter. Live values come from the driver instead, but the field
> plumbing is identical. Use the injection harness in §5 if you want to retest
> the receiver without a game.

### `event=latency-layer-status` — lifecycle / availability events

Event names you may see (status): `create-instance`, `negotiate`,
`get-device-proc-addr`, `create-device`, `create-swapchain`, `present`,
`latency-marker`, `latency-marker-coverage`, `latency-timing-unavailable`,
`latency-timing-empty`, `latency-sleep-mode-set`,
`latency-sleep`, `latency-queue-out-of-band`, `latency-stream-stale`,
`present-flow`,
`latency-sleep-mode-reapplied-create`, `latency-recovery-disable-sleep-mode`,
`latency-recovery-reapply-sleep-mode`, `latency-recovery-reset-sleep-mode-enter`,
`latency-recovery-reset-sleep-mode`, `latency-recovery-unavailable`,
`destroy-swapchain`.

### Field cheat-sheet for the two fixes

| Field | What it tells you |
|-------|-------------------|
| `present_id` | **Must advance** frame-to-frame. Frozen `present_id` while `present` counters climb = the driver's Reflex ring went stale. |
| `driver_report_duplicate_count` | `>0` → the receiver saw a repeated `present_id` and **dropped** that sample. Sustained growth = stale stream. |
| `quality=stale-driver-report` | The stale-detection fired; the meter is intentionally reporting `*-p95=n/a` instead of a frozen value. **This is correct behavior**, not a bug. |
| `latency-recovery-disable-sleep-mode` / `latency-recovery-reapply-sleep-mode` | The layer saw a sustained duplicate `present_id` run, toggled Reflex sleep mode off with a valid struct, then replayed the game's last saved sleep-mode state. `result=0` means Vulkan accepted that call. |
| `latency-recovery-reset-sleep-mode-enter` / `latency-recovery-reset-sleep-mode` | Only emitted when `PENGUIN_BURNER_LATENCY_RECOVERY_RESET=1` is set. This crash-test path calls `vkSetLatencySleepModeNV(..., nullptr)` before replaying the saved state. If `*-enter` is the final line before the game exits, the reset call did not return. |
| `latency-sleep-mode-reapplied-create` | A new swapchain was created after the game had already set Reflex sleep mode, so the layer replayed that state immediately after creation. |
| `latency-recovery-unavailable` | Stale recovery wanted to run, but no prior sleep-mode state or no `vkSetLatencySleepModeNV` function was available. The meter should keep treating Reflex as stale. |
| `latency-stream-stale` | Snapshot emitted at the stale threshold before recovery. Compare `present_count`, `last_vulkan_present_id`, latest marker IDs, and `last_driver_report_present_id` to see which side stopped advancing. |
| `latency-sleep` | `vkLatencySleepNV` was called. `sleep_value` is the timeline value passed by the game/VKD3D path. |
| `latency-queue-out-of-band` | `vkQueueNotifyOutOfBandNV` was called. `queue_type` identifies render vs present out-of-band queue type. |
| `present-flow` | Extra present snapshot emitted only with `PENGUIN_BURNER_LATENCY_DEBUG_FLOW=1`; useful when checking whether Vulkan `VkPresentIdKHR` / `VkPresentId2KHR` IDs advance. |
| `latest_marker_present_id` / `last_*_present_id` | Last Reflex marker IDs seen by the layer. If these advance past `last_driver_report_present_id`, the app/VKD3D side is still feeding markers but the driver report ring is stuck. |
| `present_mode_name` | Vulkan swapchain present mode seen at creation (`IMMEDIATE`, `MAILBOX`, `FIFO`, `FIFO_RELAXED`, or `UNKNOWN`). Use this to verify `VKD3D_SWAPCHAIN_PRESENT_MODE=IMMEDIATE` actually reached VKD3D. |
| `swapchain_latency_mode` | Whether `VkSwapchainLatencyCreateInfoNV(latencyModeEnable=true)` was visible on `vkCreateSwapchainKHR`. If false, layer ordering may hide the DXVK-NVAPI create-info patch from this observer. |
| `live_swapchain_count` | Number of live Vulkan swapchains for the device after the lifecycle event or at the flow snapshot. If this rises above 1 around the stale point, VKD3D-Proton's multi-swapchain Reflex guard is a likely trigger. |
| `gpu_render_us` | The headline metric: per-frame GPU render time = `gpu_render_end_us - gpu_render_start_us`. This is the adaptive control signal (target ~16.6 ms). |
| `gpu_render_start_us` / `gpu_render_end_us` | Raw driver timestamps. **If both are persistently 0**, the driver isn't filling them on this stack and `gpu_render_us` will be 0 / `gpu-render-p95=n/a`; `render-submit-p95` can be logged as a weaker diagnostic, but it is not the same pre-frame-generation GPU render metric. |
| `quality` | Confidence ladder (low→high): `present-frametime` < `driver-timing`/`reflex-marker*` < `reflex-render-submit` < `reflex-input-present`. |
| `marker_bits` | Bitmask of which Reflex markers the game set (see `marker_bit()` in the layer). `0` means the game isn't driving Reflex markers. |

---

## 5. Verifying the two fixes specifically

### Fix A — per-frame emission (no stuck constant)

1. Set `PENGUIN_BURNER_LATENCY_RAW_TIMING_LOG=all`.
2. Capture ~30 s of `event=latency-raw` lines while actively playing.
3. Confirm `present_id` **strictly increases** across consecutive lines and the
   timing fields (`gpu_render_us`, `render_submit_us`, …) **vary** frame to frame.
   - PASS: values move (e.g. 13.8 → 14.2 → 15.9 ms).
   - FAIL (regression): same numbers repeat with a non-advancing `present_id` and
     `driver_report_duplicate_count` staying 0 → emission/dedup logic broke.
4. Trigger a stale state (open a menu / change DLSS-FG mode / alt-tab). You should
   see `driver_report_duplicate_count` climb and the **meter** switch to
   `quality=stale-driver-report ... gpu-render-p95=n/a`. That is the intended
   handoff — a frozen number must **not** keep being reported as live.

Quick extraction:

```bash
journalctl -u PenguinBurner.service --since '5 min ago' -o cat \
  | grep -oE 'present_id=[0-9]+ .*gpu_render_us=[0-9]+' | head -40
```

#### No-GPU receiver retest harness

If you only need to exercise the **receiver** (parsing, meter, stale-detection)
without a game, inject synthetic datagrams. This runs anywhere:

```bash
python - <<'PY'
import json, socket, time
from latency_telemetry import start_latency_telemetry_logger
from latency_telemetry.receiver import latency_socket_path

lines = []
logger = start_latency_telemetry_logger(log=lines.append)
logger.raw_log_interval_s = 0.0   # log every sample
logger.log_interval_s = 0.0       # summarize immediately

s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
path = str(latency_socket_path())

# 3 fresh frames (advancing present_id) ...
for pid_frame, gpu in [(841, 13800), (842, 14200), (843, 15900)]:
    s.sendto(json.dumps({
        "type": "timing", "measurement": "driver-report", "pid": 999,
        "present_id": pid_frame, "quality": "reflex-input-present",
        "driver_report_duplicate_count": 0, "gpu_render_us": gpu,
        "render_submit_us": 2100, "input_to_present_us": 31000,
        "gpu_frame_time_us": 16670,
    }).encode(), path)
    time.sleep(0.05)

# ... then a STALE frame (duplicate_count>0) → meter must report stale
s.sendto(json.dumps({
    "type": "timing", "measurement": "driver-report", "pid": 999,
    "present_id": 843, "quality": "reflex-input-present",
    "driver_report_duplicate_count": 9000, "gpu_render_us": 15900,
}).encode(), path)
time.sleep(0.5)
logger.close(); s.close()

for l in lines:
    if "event=latency-" in l: print(l)
PY
```

Expect 3 `latency-raw` lines with advancing `present_id`, a meter line showing
`gpu-render-p95=15.90ms`, and — once only the duplicate remains in the window —
`quality=stale-driver-report ... gpu-render-p95=n/a`.

### Fix B — `gpu_render_us` is real and non-frozen

```bash
journalctl -u PenguinBurner.service --since '5 min ago' -o cat \
  | grep -oE 'gpu_render_us=[0-9]+' | sort | uniq -c | sort -rn | head
```

- Many distinct values → good, it's tracking real GPU load.
- A single value with a huge count → either genuinely steady load **or** a stale
  ring (cross-check `present_id` and `driver_report_duplicate_count`).
- All `gpu_render_us=0` → driver not populating `gpuRenderStart/End` on this
  title/stack; keep `render-submit-p95` as a diagnostic only and note that the
  requested metric is unavailable.

### Known RE9 result from 2026-06-09

RE9 can produce short fresh windows, including `gpu-render-p95=16.59ms` after a
swapchain recreation, but the stream then stalls again. In the submit+present
DXVK-NVAPI injection run, the last swapchain emitted about 59 advancing reports
(`present_id=359` through `418`) before collapsing to IDs `483..487` repeating.
The final state was `quality=stale-driver-report ... gpu-render-p95=n/a`.

The following recovery experiments were already tried and did not produce a
sustained live stream:

- replaying the captured `vkSetLatencySleepModeNV` state;
- toggling saved sleep mode off, then replaying it;
- `PENGUIN_BURNER_LATENCY_RECOVERY_RESET=1`, which made RE9 exit before the
  reset result was logged;
- DXVK-NVAPI `DXVK_NVAPI_VKREFLEX_INJECT_PRESENT_FRAME_IDS=1`;
- DXVK-NVAPI submit+present frame-ID injection.

### RE9 flow diagnosis helper

For a live RE9 attempt, start a durable capture before launching the game:

```bash
penguin-burner-steam-launch-check
penguin-burner-latency-capture --output ~/.cache/penguin-burner/latency-captures/re9-live.log
```

Stop it with `Ctrl-C` after the game exits. If the desktop hard-freezes and has
to be reset, inspect the captured log after reboot:

```bash
penguin-burner-latency-flow ~/.cache/penguin-burner/latency-captures/re9-live.log
```

For post-run analysis directly from the journal, classify the captured flow with:

```bash
journalctl -u PenguinBurner.service --since '10 min ago' -o cat --no-pager \
  | rg 'create-swapchain|destroy-swapchain|latency-stream-stale|present-flow|latency-sleep|latency-queue-out-of-band|latency-raw|latency-meter' \
  | penguin-burner-latency-flow
```

Expected useful outcomes:

- `root_cause=no-stall-detected-with-immediate-present-mode`: the
  `VKD3D_SWAPCHAIN_PRESENT_MODE=IMMEDIATE` workaround is a candidate, but only if
  the capture spans the menu-to-gameplay transition that previously stalled.
- `root_cause=vkd3d-multi-swapchain-reflex-guard`: `live_swapchain_count > 1`
  when stale; likely needs a VKD3D-Proton patch/test build or avoiding the
  setting that creates a second swapchain.
- `root_cause=nvidia-reflex-timing-ring-stale`: markers and Vulkan present IDs
  advanced while `vkGetLatencyTimingsNV` repeated the old report; do not display
  the frozen Reflex value.

---

## 6. If the layer isn't loading / no samples at all

Walk the chain top to bottom:

```bash
# 1. Loader discovery (no game):
python penguin_burner.py --check-latency-layer

# 2. Is the game process actually loading the .so?
#    While the game runs:
cat /proc/$(pgrep -f re9.exe | head -1)/maps | grep -i penguinburner_latency

# 3. Did the layer reach create-device / see the extension?
journalctl -u PenguinBurner.service -o cat | grep -E 'status=(create-device|negotiate|latency-timing)'
#    - status=latency-timing-unavailable  → vkGetLatencyTimingsNV not resolved
#      (NVAPI/Reflex not active; check DXVK_NVAPI_VKREFLEX / PROTON_ENABLE_NVAPI)
#    - status=latency-timing-empty        → extension present but driver returned
#      no timings yet (game may not be driving Reflex markers)

# 4. Socket rendezvous — are layer and daemon on the same path?
ls -la /run/user/$(id -u)/penguin-burner/latency.sock ~/.cache/penguin-burner/latency.sock 2>/dev/null
```

---

## 7. Crash / instability triage (should NOT happen post-rip-out)

The injection paths that caused freezes/OOMs are gone. If you still see a GPU hang
or VRAM OOM with the layer enabled, capture evidence and treat it as a new bug:

```bash
# user-space crash (game/Proton/driver):
coredumpctl list --since '15 min ago'
coredumpctl info <PID> --no-pager | head -60

# kernel GPU faults / resets (a real hang shows NVRM Xid):
journalctl -k --since '15 min ago' -o short-iso \
  | grep -iE 'NVRM|Xid|nvidia|gpu|reset|oom|killed'

# VRAM usage over time:
nvidia-smi --query-gpu=memory.used,memory.total --format=csv -l 1
```

Note in the bug report whether an `Xid`/reset appeared (kernel-level hang) vs only
a user-space SIGSEGV (game/driver crash) — they point at very different causes.

---

## 8. Reference

- Layer source: `native/latency_layer/src/penguinburner_latency_layer.cpp`
  - per-frame emit + stale re-emit: `query_latency_timing()`
  - `gpu_render_us` computation + JSON: `send_timing_sample()`
- Receiver/meter/log formatting: `latency_telemetry/receiver.py`
- Loader check: `latency_telemetry/layer_check.py`, CLI `--check-latency-layer`
- Manifest (enable env): `native/latency_layer/VkLayer_PENGUINBURNER_latency.json.in`
- History / rationale: `docs/re9-latency-experiment-findings.md`,
  `docs/auto-profile-latency-findings.md`
