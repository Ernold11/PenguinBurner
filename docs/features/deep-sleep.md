# Laptop Deep Sleep (RTD3 / D3cold)

On hybrid-graphics laptops (Intel/AMD iGPU + NVIDIA dGPU), the NVIDIA GPU can
power off completely while nothing uses it — NVIDIA calls this runtime D3
(RTD3); the kernel reports it as the `suspended` / D3cold state. Any program
that keeps an NVML session or a `/dev/nvidia*` file handle open pins the GPU
awake and silently drains the battery. Most GPU tools on Linux do exactly
that, including their background services.

PenguinBurner's daemon is deep-sleep aware. It does not enable RTD3 itself;
the NVIDIA driver and kernel must already expose working runtime power
management. On RTD3-capable machines the daemon holds **no** GPU handles while
the GPU is idle, so the dGPU can reach D3cold with
`penguin-burnerd.service` running.

## How it works

At startup the daemon classifies the machine using only cached kernel state
(reads that never wake the GPU):

- `/proc/driver/nvidia/gpus/<pci-addr>/power` — the driver's resolved
  `Runtime D3 status`;
- `/sys/bus/pci/devices/<pci-addr>/power/control` — must be `auto` for the
  kernel to suspend the device;
- `/sys/bus/pci/devices/<pci-addr>/power/runtime_status` — the live power
  state. A GPU ever observed `suspended` selects Mobile handling even if the
  config files were inconclusive.

**Desktop mode** (runtime D3 disabled or unsupported) keeps the always-attached
behavior: the saved profile is applied at boot and the daemon stays attached.

**Mobile mode** (runtime D3 enabled with automatic runtime power management)
gets the deferred behavior:

- A saved profile is not applied while the GPU sleeps. The daemon watches
  `runtime_status` once per second (a cached read — no GPU traffic) and
  applies the profile when the GPU is actually in use: sustained `active`
  plus a real GPU client (a game, not a transient Vulkan capability probe).
- What counts as a real client follows the driver's runtime D3 granularity.
  Under fine-grained RTD3 (`mode: "fine-grained"`) the driver lets the GPU
  sleep even while desktop shells or monitoring tools hold `/dev/nvidia<N>`
  open, so an open handle proves nothing — the daemon asks NVML for live
  graphics/compute contexts instead. Under coarse-grained RTD3 any
  `/dev/nvidia<N>` holder keeps the GPU awake, so there the device-handle
  scan is the accurate signal (auxiliary `nvidiactl`/`nvidia-uvm` handles
  never count).
- One-off telemetry or capability queries release their GPU handles after 30
  idle seconds instead of keeping them for the daemon's lifetime.
- GPU persistence mode is never enabled (it blocks runtime D3); the profile
  is reapplied on wake instead.

After upgrading from a version that previously enabled persistence mode,
reboot once before testing deep sleep. NVIDIA persistence state can outlive a
service restart, and PenguinBurner does not make implicit hardware writes to
undo old state.

## Checking it on your machine

```
penguin-burner-cli --daemon-status
```

The `deep_sleep` block reports the verdict:

```json
"deep_sleep": {
  "state": "mobile",
  "mode": "fine-grained",
  "pci_addr": "0000:01:00.0",
  "runtime_status": "suspended",
  "autostart_deferred": true,
  "suspended_observed": true
}
```

`state: "mobile"` means the daemon treats GPU handles as ephemeral;
`"desktop"` (with a `reason`) means always-attached behavior. `"unknown"`
stays detached like Mobile mode until detection resolves. To confirm the GPU
really sleeps with the service running:

### Who is using the GPU right now

In Mobile mode the block also carries `gpu_clients` — the daemon's latest
sample of the processes it judged the park/wake decision by:

```json
"gpu_clients": {
  "source": "nvml-contexts",
  "graphics_count": 1,
  "compute_count": 0,
  "device_node_count": 2,
  "total_count": 1,
  "age_s": 0.6,
  "graphics": [{"pid": 3117, "name": "vkcube"}],
  "compute": [],
  "device_node_holders": [
    {"pid": 1450, "name": "plasmashell"},
    {"pid": 3117, "name": "vkcube"}
  ]
}
```

- `graphics` / `compute` list the processes holding a live NVML context of
  that kind; `device_node_holders` lists every process with `/dev/nvidia<N>`
  open. Each entry is `pid` plus the process name (`null` if it exited
  before the lookup).
- `source` names the decisive signal: `"nvml-contexts"` under fine-grained
  RTD3, `"device-nodes"` under coarse-grained RTD3 or when the NVML process
  query is unavailable.
- `total_count` is the number of unique clients the decision counted.
  **Parking proceeds only while it stays 0**, so if the GPU never parks,
  the named processes here are what is keeping it awake.
- Under fine-grained RTD3 the `device_node_holders` list is informational:
  a desktop shell holding the device open does not keep the GPU awake and
  does not block parking — which is why `total_count` can be 0 while the
  list is not empty.
- `age_s` is the sample's age in seconds. It grows while the GPU sleeps,
  because sampling only happens while the GPU is awake; a large value next
  to `runtime_status: "suspended"` is normal.

The daemon also writes the same evidence to its journal whenever the client
sample changes — process starts and exits, not once per second — so an idle
system stays quiet and the log reads as a timeline of who took and released
the GPU:

```
deep sleep: gpu clients (nvml-contexts): graphics=1 [3117 vkcube], compute=0, device-node holders=2 [1450 plasmashell, 3117 vkcube]
```

To watch it live while reproducing a park/wake problem:

```
sudo journalctl -u penguin-burnerd.service -f
```

To capture a log file to attach to a bug report (adjust the window to cover
your test):

```
sudo journalctl -u penguin-burnerd.service --since "-1 hour" --no-pager > penguin-burnerd-deep-sleep.log
```

```
cat /sys/bus/pci/devices/0000:01:00.0/power/runtime_status
```

(replace the address with your dGPU's; it should read `suspended` when no
game is running).

If `state` is `"desktop"` with reason `power/control is not 'auto'`, your
distribution has not enabled runtime power management for the dGPU — see the
NVIDIA driver README chapter on runtime D3 for the required udev rules and
module options.

## Applied profiles and deep sleep coexist

An applied profile does not keep the GPU awake. While the profile runtime
is attached it polls the driver, which by itself prevents runtime D3 — so
when no other process has really used the GPU for 60 seconds (the same
mode-aware client signal as above), the daemon **parks** the runtime: the
engine releases every GPU
handle (fans return to hardware auto, the clock lock is released) while
the profile stays applied as a standing intent. The GPU is then free to
enter D3cold. When something real starts using the GPU again, the profile
re-attaches and reapplies within a few seconds — before a game finishes
loading. `deep_sleep.parked: true` in `--daemon-status` shows a parked
profile; `autostart_deferred: true` confirms it will reapply on use. If a
profile never parks, `gpu_clients` (above) names the processes still being
counted as GPU users.

Restoring stock behaves the same way, with one refinement: a stock
runtime enforces nothing, so once parked it never re-attaches at all.

## Current limitations

- Profile reapply after a deep-sleep cycle happens when the runtime is
  started for the wake, not transparently mid-session.
- After an Auto-UV scan or verification finishes, the saved profile applies
  at the next real GPU use instead of immediately, so the scan's end never
  pins a GPU that is about to idle.
- A runtime you explicitly stop stays stopped: the boot profile arms the
  wake-apply at most once per daemon start.
