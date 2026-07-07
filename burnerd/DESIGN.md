# penguin-burnerd (Rust) — design & mechanics

The root daemon of PenguinBurner, rewritten in Rust. One static-ish binary that
holds all privilege: it serves the existing unix-socket JSON API, applies
runtime profiles natively via NVML/NVAPI FFI, runs the fan-curve and adaptive
loops, publishes telemetry for the GUI and the C++ overlay, and spawns the
(unchanged, Python) Auto-UV scan as a child process.

Behavioral ground truth for the port lives in `port-notes/` (specs 01–09 —
git-ignored working notes generated from the Python sources at this branch
point; the Python code in git history remains the ultimate reference).
**Rule zero: parity over elegance.** The
proven profile/auto-UV behavior must not change. Where this doc and a spec
disagree on observed behavior, the spec wins.

Licensing note: architectural inspiration was taken from studying other
open-source Linux GPU daemons; all code here is written from scratch. Do not
copy code from other projects and do not reference them in code or comments.

## Process & privilege model

```
systemd ──▶ penguin-burnerd (Rust, root)
             ├─ socket server        /run/penguin-burnerd.sock (JSON lines)
             ├─ profile engine       in-process thread (NVML/NVAPI writes)
             └─ auto-UV scan child   Python CLI, spawned on demand (unchanged)

GUI (Python, user)  ──socket──▶ daemon        (separate process, crash-isolated)
CLI (Python, user)  ──socket──▶ daemon        (separate process)
C++ overlay (game)  ──files──▶ telemetry      (reads state files, unchanged)
```

- The GUI/CLI ↔ daemon boundary is untouched: unprivileged clients over the
  socket, gated by `SO_PEERCRED` (uid 0 or `PENGUIN_BURNER_DAEMON_ALLOWED_UID`).
- The profile engine moves **in-process** (vs today's root Python child). A
  panic in the engine is caught at the thread boundary (`catch_unwind`) and
  reported as `runtime_profile_stopped`; a hard NVML wedge is recovered by the
  systemd watchdog (below) — which is strictly better than today, where a
  wedged child goes undetected.
- The Auto-UV scan keeps its own process: it is the component that
  deliberately drives the GPU toward instability, and its Python logic is
  frozen (proven curves).

## Threading model

std threads only — no async runtime. NVML calls are slow and clients are few.

| Thread | Role |
|---|---|
| main | init, bind+chmod socket, accept loop |
| per-connection | JSON-lines request loop (mirrors `ThreadingUnixStreamServer` w/ daemon threads) |
| engine | profile loop: poll → adaptive → fan → guards → telemetry → sleep (parity with spec 02/04) |
| scan drain/monitor | stream child stdout to the requesting client; detached kill ladder on disconnect |

Shared state: one `Mutex<Supervisor>` (job enum: `Idle` / `Scan{..}` /
`Profile{..}` + generation counter). Lock scopes stay tiny; `status` takes the
mutex briefly. The generation counter reproduces the Python identity guard that
prevents a stale scan monitor from clobbering a newer job.

Panic policy: `panic = "unwind"` + `catch_unwind` at every thread entry. A
connection or engine panic must never take the daemon down.

## Wire protocol (compatibility contract)

Same socket path, same framing, same methods, same field names — the existing
`runtime/daemon_client.py`, GUI, and CLI must work unchanged (spec 01 is the
contract; spec 08 lists what each caller actually reads).

- Framing: newline-delimited compact JSON (`separators=(",", ":")` equivalent),
  UTF-8, socket mode `0o666` (auth = peercred, not file perms).
- Methods: `status`, `start_runtime_profile`, `stop_runtime_profile`,
  `stop_auto_uv_scan` (request/response) and `start_auto_uv_scan` (streaming:
  `{"ok":true,"control":"started","pid":N}` → `{"ok":true,"line":...}*` →
  `{"ok":true,"control":"finished","exit_code":N}`).
- State strings: `idle`, `auto_uv_scan_running|stopped`,
  `runtime_profile_running|stopped` — scan checked before profile.
- `active_job` for a runtime profile: `pid` = the daemon's own pid (engine is
  in-process), `returncode` = `null` while running, `0` after clean stop, `1`
  after an engine error. Verify against spec 08 that no caller assumes the pid
  is a *different* process.
- argv whitelists and value formatting are load-bearing: runtime-profile flags
  (`--auto-uv-profile`, `--silent-fan-curve`, `--adaptive-auto-uv`,
  `--gpu-index`, `=`-forms included), Auto-UV option→flag map, bool → `"1"/"0"`
  **before** numeric handling, float → `%.6g`.
- Load-bearing error strings (tests/parsers match them) are reproduced verbatim
  per spec 01.

## Supervisor mechanics

- **Autostart:** on start, read `/var/lib/penguin-burner/last-runtime.json`
  (`{"argv": [...], "program_file": "..."}`) and start the engine with those
  args. **Simplification vs Python:** the base64 env autostart path
  (`PENGUIN_BURNER_DAEMON_AUTOSTART_ARGV_B64`) is dropped; `install-systemd`
  seeds the state file instead. `program_file` is still *written* for
  downgrade compatibility and ignored on read.
- `start_runtime_profile` validates argv against the whitelist, stops any
  running engine, starts a new one, persists the state file (last action wins
  across reboots). `stop_runtime_profile` stops the engine but does **not**
  clear the file (parity: a daemon restart re-runs the last action; only
  install/uninstall clear it).
- Scan lifecycle (parity with spec 01): refuse if a scan is running; clear a
  stale stop-request file; stop the engine; spawn
  `<python> <program_file> --auto-uv-voltage-scan --json-events
  --auto-uv-require-final-choice <mapped options>`; stream stdout lines.
  Client disconnect mid-scan → write `abort-final-choice` stop file + SIGINT +
  detached monitor (30 s → SIGTERM, 5 s → SIGKILL, 5 s). `stop_auto_uv_scan`
  → `offer-final-choice` stop file + SIGINT. After the scan releases the GPU,
  the engine is restarted from the state file. The Python CLI location comes
  from `PENGUIN_BURNER_DAEMON_PROGRAM_FILE` (kept).
- Stop-request marker lives under the **desktop user's**
  `~/.config/PenguinBurner` — effective-home resolution
  (`PENGUIN_BURNER_HOME`/`SUDO_USER`/`PKEXEC_UID`, spec 06) is ported into
  `paths.rs` and must match `common/penguin_burner_paths.py` exactly.

## Profile engine

Exact port of the Python runtime child (specs 02/04/05). Structure:

- `options.rs` — whitelisted argv → `EngineOptions` (tier, silent fan curve,
  adaptive flag, gpu index).
- `profile_store.rs` — loads the saved per-tier profile JSON and
  `auto-uv-fan-curve.json` from the effective-home config dir. **Backward
  compatible with existing user profiles — every field, including
  optional/legacy ones (spec 06).** Invalid/blocked saved fan curve silently
  downgrades `fan_control_enabled` to false (parity).
- `apply.rs` — GPU mutation ordering is load-bearing: persistence mode → VF
  per-point frequency offsets → power limit → memory offset → locked clock
  ceiling. Initial power-limit and memory-offset failures are errors; some
  later ops only log (parity per spec 02).
- `guard.rs` — VF reset guard: reapply only when drift > 1 MHz, cooldown
  `max(poll_interval, 10 s)`, sampling top-8 non-zero points; flattened clock
  ceiling logic as specced.
- `fan.rs` — curve interpolation, hysteresis, spin-up/down and zero-RPM rules,
  emergency temps (80/75 defaults), multi-fan handling; restore =
  `nvmlDeviceSetDefaultFanSpeed_v2` per fan, idempotent, on every exit path.
- `adaptive.rs` — adaptive tier state machine + target-FPS + CPU sampler,
  constants per spec 04. Switches happen before the fan decision in the same
  iteration.
- `telemetry.rs` — overlay/GUI state files **byte-compatible** (spec 05): same
  paths, tmp+rename atomicity, same field names/order/formatting, same cadence
  and throttling (status-line dedupe by bucketed signature).
- Exit cleanup (SIGTERM/stop): release fans to auto + release the clock lock,
  close handles — and deliberately **leave** the UV curve, power limit, memory
  offset, and persistence mode applied (parity; "reset to stock" is its own
  profile action).

Engine stop = atomic flag checked each iteration + `join` with timeout. Join
timeout (wedged NVML) → log loudly and `exit(1)`; systemd restarts the daemon
and the state file re-applies the last action.

## GPU backend

- `gpu/mod.rs` defines a narrow `GpuBackend` trait (reads + writes the engine
  needs). The engine is tested against a mock; `NvmlBackend` is the real impl.
- NVML via the `nvml-wrapper` crate (dlopens `libnvidia-ml.so.1`; one global
  init, short-lived device handles). Symbols the crate doesn't expose (VF
  offsets, min/max VF offset queries, etc.) are loaded with `libloading` in
  `gpu/nvml_raw.rs` with the exact fallback chains from spec 03 (`_v3`→`_v2`,
  clocks-event vs throttle reasons, …).
- Hidden NVAPI (`libnvidia-api.so.1`, `nvapi_QueryInterface` by numeric id,
  version-tagged `#[repr(C)]` structs) in `gpu/nvapi.rs` for per-point VF
  curve control and voltage — struct layouts per spec 03, with **compile-time
  size assertions** for every struct (the driver rejects wrong sizes).
- Unit discipline (spec 03): NVML VF offsets are signed MHz, hidden-NVAPI curve
  is kHz; power is mW at FFI, with the two rounding conventions preserved.
  Mem-clock VF offset writes can silently not stick — read-back is required.
  SetGpuLockedClocks single-lock passes min=max; range mode snaps min with
  ceil-preference, max with floor-preference, then clamps.
- Error mapping preserves the Python distinctions: which paths raise typed
  errors vs return best-effort `None` vs log-and-continue, and the exact
  load-bearing message texts.

## systemd unit

Generated by `install-systemd` (Python side, spec 07), updated to:

- `ExecStart=<path>/penguin-burnerd` (binary discovery: explicit flag →
  `/usr/libexec/penguin-burnerd` → dev build under `burnerd/target/release/`).
- `Environment=PENGUIN_BURNER_DAEMON_PROGRAM_FILE=…` (Python CLI, for scan
  children) and `…_ALLOWED_UID=…` (kept). The `ARGV_B64` env is gone; the
  installer seeds `/var/lib/penguin-burner/last-runtime.json` instead.
- `Type=notify` + `WatchdogSec=30`: the daemon sends `READY=1` and the engine
  heartbeats `WATCHDOG=1` (hand-rolled `sd_notify` datagram — no libsystemd
  dependency). `Restart=on-failure`.

## Build, packaging, dependency policy

- Separate module dir `burnerd/` (this dir), plain cargo project. Edition
  2021, `rust-version` pinned conservatively for distro toolchains,
  `Cargo.lock` committed.
- Release profile: `lto = true`, `codegen-units = 1`, `strip = true`,
  `panic = "unwind"` (see panic policy).
- Dependencies (deliberately minimal; audited with `cargo-deny`): `serde`,
  `serde_json`, `nvml-wrapper`, `libloading` (shared with nvml-wrapper),
  `libc`, `anyhow`. Tiny hand-rolled stderr logger. No tokio, no D-Bus.
- Checks that must pass before any commit touching `burnerd/`:
  `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`,
  `cargo test`, `cargo deny check`.
- Packaging: PKGBUILD/deb/RPM/flatpak build from source (cargo); pip package
  stays pure Python.

## Testing strategy

1. **Rust unit tests** — protocol parsing, argv whitelists + value formatting,
   fan interpolation, VF plan math, persistence round-trips, engine logic
   against the mock backend.
2. **Golden protocol tests (the parity anchor)** — pytest spawns the built
   binary on a temp socket with `PENGUIN_BURNER_DAEMON_PROGRAM_FILE` pointed at
   a stub script, then drives it with the *real* `daemon_client.py`, porting
   the behaviors `tests/test_daemon_api.py` pins. Skipped when the binary
   isn't built.
3. **On-hardware verify** — apply each tier through the daemon and read back
   live NVML state (VF offsets, locked clocks, power limit, fan), restart the
   daemon and confirm the state file re-applies, run a scan start/stop through
   the socket.

## Simplifications vs the Python daemon (LoC cuts)

1. Base64-env autostart path deleted → single state-file mechanism.
2. Profile engine in-process → no self-exec, no program-file resolution for
   profiles, no child-pid bookkeeping, no argv re-marshalling round-trip.
3. Two module-global process vars + lock → one supervisor state enum.
4. Watchdog replaces "hope the child is fine" — and adds hang detection today's
   design lacks.
5. Milestone A deletes the Python daemon *engine*: the four engine modules of
   `runtime/gpu_control/` (adaptive_profile_runtime, overlay_state_publisher,
   process_cpu_sampler, vf_curve_runtime_policy), all of `runtime/fan_control/`,
   the CLI foreground apply path, and the `daemon_api.py` socket server — ~4.1k
   LoC, replaced by this crate. The scan/verify/installer support that outlives
   the engine stays until milestone B (de-roots the scan): the other 7
   `runtime/gpu_control/` modules, `runtime/stability_test/`, `runtime/support/`
   incl. `runtime_service.py`'s installer, `drivers/nvidia/*`, and
   `daemon_client.py`.

## Milestones

- **A0** scaffold + toolchain gates — **A1** supervisor/protocol golden-tested
  — **A2** GPU backend — **A3** engine/fan/adaptive/telemetry — **A4**
  integration (installer, packaging), full pytest + cargo suites green,
  hardware verify.
- **B** (approved; map in port-notes/14): low-level GPU *write* RPCs on the
  socket; `drivers/nvidia` write half re-pointed to them so auto-UV sweep logic
  stays byte-identical Python while all mutations are implemented once, in
  Rust; then de-root the scan child. Wire spec below.

## Milestone B wire spec (same socket, same framing, additive methods)

GPU writes (all unary; every method takes `gpu_index`; errors relay the exact
backend error text in the standard `{"ok":false,"error":…}` envelope — one
Python consumer pattern-matches error strings):

| method | request extras | result |
|---|---|---|
| `gpu_apply_vf_offsets` | `offsets: [[index, offset_khz], …]` | `{"applied": N}` — daemon owns GET→mutate→SET; non-listed points preserved (existing `apply_offsets_khz`) |
| `gpu_apply_power_limit` | `power_limit_w` | `{"applied_w": N}` |
| `gpu_apply_clock_offsets` | `gpc_clk_vf_offset_mhz?`, `mem_clk_vf_offset_mhz?` | readback dict (mem readback required — may silently not stick) |
| `gpu_apply_locked_core_clock` | `clock_mhz` (+snap opts) | snap dict, parity with the Python return |
| `gpu_apply_locked_core_clock_range` | `min_mhz`, `max_mhz` (+snap opts) | snap dict |
| `gpu_reset_locked_core_clocks` | — | `{"reset": true}` |
| `gpu_reset_locked_memory_clocks` | — | `{"reset": true}` |
| `gpu_enable_persistence_mode` | — | `{"enabled": true}` |

Privileged-flow migrations: `start_profile_verification` (streaming,
scan-style child with its own argv whitelist) + `stop_profile_verification`;
`delete_auto_uv_profiles` (`paths: […]` — canonicalized and prefix-enforced
against the effective user's saved-UV dir; the daemon is root, so path
validation is a security boundary, not a nicety).

As implemented (B1):

- `start_profile_verification` options (whitelist, scan-option semantics):
  `stability_seconds`, `gpu_index`, `auto_uv_profile` — child argv is
  `--stability-test <options in that order> --stability-stop-request-file
  <config>/profile-verify-stop-requested` (marker owned by the daemon; cleared
  on start, written by `stop_profile_verification`/disconnect before SIGINT).
  Same started/line/finished framing, single child slot shared with the scan
  (mutual refusal in both directions, runtime-profile start refused too),
  same disconnect kill ladder. New status strings while it runs:
  `profile_verification_running|stopped`, job type `profile_verification`.
- Children are spawned with a reset signal mask (the daemon blocks
  SIGINT/SIGTERM process-wide for its signal thread; without the reset the
  stop SIGINT would never be delivered to scan/verification children).
- `delete_auto_uv_profiles`: every path must canonicalize (symlinks resolved)
  to a `*.json` regular file that is a DIRECT child of the canonical
  `<home>/.config/PenguinBurner/auto-uv-profiles` (the dir suffix itself must
  be symlink-free; home may be, e.g. ostree). Validate-all-then-delete;
  unlink via a pinned dir fd (`unlinkat`); any rejection errors naming the
  offending path. Result `{"deleted":[…]}` (canonical paths).

Backend for RPC writes: one lazy shared `NvmlBackend` per `gpu_index` in the
server (NVML is refcounted/thread-safe; NVAPI get-mutate-set is per-call),
serialized under one mutex. Writes are legal in any supervisor state — during
a scan the engine is stopped by design, and the scan child is the only
intended caller. Test seam: `PENGUIN_BURNERD_TEST_MOCK_GPU=1` swaps the lazy
backend for the in-memory mock and echoes recorded ops as `mock_ops` in
results (see `gpu_rpc.rs`).

De-root (after B1+B2 verify): `scan.rs` spawns the child via
`pre_exec` (setsid → initgroups → setgid → setuid from
`PENGUIN_BURNER_DAEMON_ALLOWED_UID` / `Q2RTX_UID/GID`) with `HOME`/XDG env
injected; Python then never runs as root. Flatpak parity: build the daemon
into `/app/libexec` (rust SDK extension + vendored crates); the existing
elevated install step copies it to `/usr/libexec` — unit generation unchanged.
- Deferred with a designed hook: a compute stability workload that actually
  catches high-clock instability (the current CUDA shader doesn't) — proposal
  doc to follow; the engine exposes a clean job slot for it.
