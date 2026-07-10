# penguin-burnerd design

`penguin-burnerd` is PenguinBurner's single root-owned GPU authority. The
Python application decides what the user asked for; the Rust daemon validates
and executes that resolved intent without rereading user profile files.

The detailed migration decisions are recorded in
`docs/superpowers/specs/2026-07-10-rust-daemon-simplification-design.md`.

## Boundary

```text
Python GUI / CLI (user)
  - profile selection and tier assignment
  - Auto-UV search and final verification
  - fan/profile/config interpretation
  - builds an immutable RuntimeSpec
              |
              | JSON-lines Unix socket
              v
penguin-burnerd (root, systemd)
  - validates RuntimeSpec and GPU UUID
  - performs NVML/NVAPI reads and writes
  - runs the active fan/adaptive/telemetry loop
  - supervises temporary scan/verification children
```

There is no second long-lived Python worker. Scan and verification children are
temporary, occupy the daemon's single child slot, and are stopped when their
client disconnects. Closing the GUI does not abandon a worker.

## RuntimeSpec

`apply_runtime_spec` replaces the old runtime argv API. A spec contains:

- the selected GPU's stable UUID plus its index at resolution time;
- `stock`, `static`, or `adaptive` mode;
- fully resolved V/F points, memory offset, power limit, and clock ceiling;
- fully resolved fan settings and whether fan control is enabled;
- adaptive tier profiles and policy values;
- persistence-mode and overlay settings.

Serde rejects unknown fields and the daemon validates ranges and cross-field
invariants before stopping the current engine. The daemon then checks that the
UUID still matches the device at the supplied index before any write.

The apply response is synchronous: `started: true` is returned only after the
initial GPU writes and readbacks succeed. If apply fails, the supervisor first
restores the previous spec and then tries a stock fallback. A wedged initializer
is retained in the supervisor so another GPU writer cannot start over it.
Startup replay follows the same rule: once a valid persisted spec reaches the
GPU, a failed initial apply is restored to stock on that GPU before the socket
starts accepting work.

## State and restart behavior

Two states are intentionally separate:

- `/run/penguin-burner/active-runtime.json` is current-session recovery. A
  daemon restart in the same boot reapplies it; explicit stop removes it.
- `/var/lib/penguin-burner/boot-runtime.json` is opt-in boot persistence. It is
  written only through `set_boot_runtime_spec` by the install/persist flow.

The old `last-runtime.json` argv/program-path replay format is not read. The
installer removes it as migration cleanup.

When a scan or verification starts, the active engine is stopped but its
session spec remains. After the child exits, the supervisor reapplies that spec.

## GPU mutation ordering

The runtime applies:

1. persistence mode;
2. memory offset;
3. per-point V/F offsets;
4. power limit;
5. locked clock ceiling.

Memory must precede V/F because a coarse memory-offset write clears the
per-point V/F table on supported NVIDIA drivers. Initial memory, V/F, and power
writes are read back. Stock mode resets locked clocks, coarse offsets, the power
limit, and editable V/F offsets before reporting success.

Auto-UV uses the same Rust stock-reset pipeline through `gpu_reset_defaults`;
Python only turns the returned clean V/F snapshot into its search baseline.
There is no second Python implementation of the privileged reset sequence.

Fans and the clock lock are restored on every engine exit. The applied V/F
curve, memory offset, power limit, and persistence mode remain until another
spec changes them; stock mode is the explicit full-reset action.

## Supervisor

One `Mutex<Supervisor>` owns:

- at most one in-process runtime engine;
- at most one scan or profile-verification child;
- a child generation counter that prevents a stale monitor clearing a newer
  job.

A runtime engine and a scan/verification child never write the GPU at the same
time. Engine stop has a timeout; a timed-out engine remains registered and all
competing GPU work is refused until it really exits.

Child processes use `shared_child` for cached wait status and safe signalling.
They receive SIGINT first, then the existing TERM/KILL ladder. The daemon uses
`nix` for credentials, groups, pipes, and privilege drop.

## GPU backend

`GpuBackend` is the narrow testable interface used by the engine and RPC layer.
The production backend uses `nvml-wrapper` for documented NVML operations.

Raw FFI remains only in `gpu/nvapi.rs`, because NVIDIA's per-point V/F curve and
voltage query interface is undocumented and has no supported crate wrapper.
Its versioned structures retain compile-time size assertions.

The socket also exposes coarse typed read snapshots (`gpu_capabilities`,
`gpu_telemetry`, and `gpu_vf_snapshot`) so Python no longer opens NVML itself.

## Runtime and systemd

The daemon uses standard threads rather than an async runtime. `signal-hook`
handles shutdown signals, `sd-notify` sends readiness/watchdog messages,
`procfs` supplies process metrics, and `tempfile` provides atomic state writes.

The systemd unit executes the fixed root-owned
`/usr/libexec/penguin-burnerd`. `PENGUIN_BURNER_DAEMON_PROGRAM_FILE` points to
the Python CLI used only for temporary scan/verification children.

## Verification

Before release or installation:

```bash
cargo fmt --check --manifest-path burnerd/Cargo.toml
cargo clippy --manifest-path burnerd/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path burnerd/Cargo.toml
python -m pytest tests/ -q
```

GPU changes also require a real socket-driven apply and live NVML readback on
the selected GPU. Saved profiles must remain byte-for-byte unchanged.
