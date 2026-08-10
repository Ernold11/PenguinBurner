# Laptop Deep Sleep (RTD3 / D3cold)

On hybrid-graphics laptops (Intel/AMD iGPU + NVIDIA dGPU), the NVIDIA GPU can
power off completely while nothing uses it — NVIDIA calls this runtime D3
(RTD3); the kernel reports it as the `suspended` / D3cold state. Any program
that keeps an NVML session or a `/dev/nvidia*` file handle open pins the GPU
awake and silently drains the battery. Most GPU tools on Linux do exactly
that, including their background services.

PenguinBurner's daemon is deep-sleep aware. On RTD3-capable machines it holds
**no** GPU handles while the GPU is idle, so the dGPU can reach D3cold with
`penguin-burnerd.service` running.

## How it works

At startup the daemon classifies the machine using only cached kernel state
(reads that never wake the GPU):

- `/proc/driver/nvidia/gpus/<pci-addr>/power` — the driver's resolved
  `Runtime D3 status`;
- `/sys/bus/pci/devices/<pci-addr>/power/control` — must be `auto` for the
  kernel to suspend the device;
- `/sys/bus/pci/devices/<pci-addr>/power/runtime_status` — the live power
  state. A GPU ever observed `suspended` arms deep-sleep handling even if the
  config files were inconclusive.

**Desktops** (runtime D3 disabled or unsupported) keep the classic behavior:
the saved profile is applied at boot and the daemon stays attached.

**RTD3 laptops** get the deferred behavior:

- A saved profile is not applied while the GPU sleeps. The daemon watches
  `runtime_status` once per second (a cached read — no GPU traffic) and
  applies the profile when the GPU is actually in use: sustained `active`
  plus a real client holding a `/dev/nvidia<N>` device handle (a game, not
  a transient Vulkan capability probe, and not monitoring tools that only
  hold `nvidiactl`/`nvidia-uvm` handles).
- One-off telemetry or capability queries release their GPU handles after 30
  idle seconds instead of keeping them for the daemon's lifetime.
- GPU persistence mode is never enabled (it blocks runtime D3); the profile
  is reapplied on wake instead.

## Checking it on your machine

```
penguin-burner-cli --daemon-status
```

The `deep_sleep` block reports the verdict:

```json
"deep_sleep": {
  "state": "armed",
  "mode": "fine-grained",
  "pci_addr": "0000:01:00.0",
  "runtime_status": "suspended",
  "autostart_deferred": true,
  "suspended_observed": true
}
```

`state: "armed"` means the daemon treats GPU handles as ephemeral;
`"disabled"` (with a `reason`) means classic desktop behavior. To confirm the
GPU really sleeps with the service running:

```
cat /sys/bus/pci/devices/0000:01:00.0/power/runtime_status
```

(replace the address with your dGPU's; it should read `suspended` when no
game is running).

If `state` is `"disabled"` with reason `power/control is not 'auto'`, your
distribution has not enabled runtime power management for the dGPU — see the
NVIDIA driver README chapter on runtime D3 for the required udev rules and
module options.

## Current limitations

- After a game ends, the daemon keeps its handles until the profile is
  stopped or the daemon restarts; automatic idle re-detach is planned.
- Profile reapply after a deep-sleep cycle happens when the runtime is
  started for the wake, not transparently mid-session.
- After an Auto-UV scan or verification finishes, the saved profile applies
  at the next real GPU use instead of immediately, so the scan's end never
  pins a GPU that is about to idle.
- A runtime you explicitly stop stays stopped: the boot profile arms the
  wake-apply at most once per daemon start.
