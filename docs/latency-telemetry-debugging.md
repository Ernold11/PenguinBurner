# Latency Telemetry Debugging Guide

PenguinBurner latency telemetry is now a small Vulkan cadence observer. It uses
present pacing for the fallback signal and a read-only `vkSetLatencyMarkerNV`
observer for base-frame cadence when Reflex/VKD3D markers are available. It does
not use Reflex timing queries, frame-ID injection, GPU timestamp injection, or
stale-report recovery.

## Measurement Point

The native layer hooks `vkQueuePresentKHR` in the game process. After the next
layer/driver `vkQueuePresentKHR` returns, it records a monotonic timestamp for
each swapchain and emits the delta from the previous app-visible present:

```text
measurement=present-pacing quality=present-frametime present_frametime_us=<delta>
```

For RE9 with NVIDIA frame generation x3, static scenes can make this stream look
like base-frame cadence. Mouse/camera motion showed the important caveat:
generated/output cadence can appear in the same present stream. The headline
`present-fps` is therefore a base-cadence estimate:

- normally it follows the slow P95 frametime;
- if the stream flips to evenly paced generated/output presents, the receiver
  compares raw cadence with the last stable base cadence and divides by an
  inferred 2x/3x/4x multiplier.
- if no base evidence exists yet and the present-only fallback starts at high
  output cadence, `present-fps` stays `n/a` instead of seeding from the bad
  `120`-class number.

If the receiver starts while the stream already contains only generated/output
intervals, a present-only layer has no source of truth for true
pre-frame-generation FPS. That case needs a real base-frame signal from
Reflex/VKD3D markers.

## Build

```bash
cmake -S native/latency_layer -B native/latency_layer/build -DCMAKE_BUILD_TYPE=Release
cmake --build native/latency_layer/build
```

The build produces:

```text
native/latency_layer/build/libVkLayer_penguinburner_latency.so
native/latency_layer/build/VkLayer_PENGUINBURNER_latency.json
```

## Steam Launch Options

For RE9 on this host, use the setup helper:

```bash
penguin-burner-steam-game-setup --game re9 --wait
penguin-burner-steam-launch-check --compat-tool 'Proton-CachyOS Latest'
```

The launch line should be present-only:

```text
PENGUIN_BURNER_LATENCY_SOCKET=/run/user/1000/penguin-burner/latency.sock
VK_ADD_IMPLICIT_LAYER_PATH=/home/jp/PenguinBurner/native/latency_layer/build
VK_LOADER_LAYERS_ENABLE=VK_LAYER_PENGUINBURNER_latency
PENGUIN_BURNER_LATENCY_LAYER=1
gamemoderun %command% /WineDetectionEnabled:False
```

## Journal Output

The daemon logs a summary every 3 seconds. The final supported line is:

```text
event=latency-meter pid=<pid> quality=present-frametime samples=<n> present-frametime-p95=<ms> present-fps=<fps> raw-present-fps-avg=<fps> raw-present-fps-median=<fps> raw-present-fps-5pct-low=<fps> raw-present-fps-1pct-low=<fps>
```

Field meanings:

| Field | Meaning |
|-------|---------|
| `present-frametime-p95` | P95 app-visible Vulkan present-to-present interval in the 3 second meter window. |
| `present-fps` | Base-cadence estimate. Prefer marker-derived base cadence; otherwise use P95/deinterlace only after present-only fallback has plausible base evidence. |
| `raw-present-fps-avg` | Raw mean present FPS over the same window: counted present intervals divided by summed present frametime. With frame generation this can become output cadence. |
| `raw-present-fps-median` | Raw median present cadence from median present frametime. With frame generation this can become output cadence. |
| `raw-present-fps-5pct-low` | Raw FPS from the slowest 5% of present frametimes in the window. |
| `raw-present-fps-1pct-low` | Raw FPS from the slowest 1% of present frametimes in the window. |

The old Reflex fields are intentionally gone from the meter output:

```text
render-present-p95 gpu-render-p95 input-present-p95 gpu-frame-p95
```

## Raw Samples

Raw per-sample logging is off by default. To temporarily inspect individual
present samples from a foreground receiver:

```bash
PENGUIN_BURNER_LATENCY_RAW_TIMING_LOG=all python - <<'PY'
import time
from latency_telemetry import start_latency_telemetry_logger

logger = start_latency_telemetry_logger(log=print)
print("listening on:", [str(path) for path in logger.paths])
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    logger.close()
PY
```

Expected raw line shape:

```text
event=latency-raw pid=<pid> measurement=present-pacing device=0x... swapchain=0x... quality=present-frametime present_count=<n> present_frametime_us=<us>
```

## Verify The Service

```bash
sudo /usr/bin/systemctl restart PenguinBurner
sudo /usr/bin/systemctl status PenguinBurner
journalctl -u PenguinBurner.service -f -o cat | grep 'event=latency-meter'
```

Only `event=latency-meter` should appear during normal gameplay unless raw
logging was explicitly enabled.
