# Rust daemon port — status

Working log for the `rust_burnerd` branch. Newest entries on top.
Mechanics/architecture: see `DESIGN.md`. Behavior specs: `port-notes/`.

## Decisions locked (2026-07-07, with JP)

- Rust binary = socket supervisor + **in-process** profile engine (thread) with
  systemd watchdog heartbeat; Auto-UV scan stays an unchanged Python child.
  GUI/CLI stay separate unprivileged processes over the socket — unchanged.
- Wire protocol stays byte-compatible with `runtime/daemon_client.py`.
- Autostart simplification: base64-env path dropped; the
  `/var/lib/penguin-burner/last-runtime.json` state file is the only mechanism.
- Minimal deps, std threads, no tokio. Simplicity target: come out *smaller*
  than the ~6k Python LoC being replaced.
- After parity + hardware verify: delete the Python engine
  (runtime/gpu_control, fan_control, runtime_service run path, daemon_api
  server). `daemon_client.py` stays.
- Milestone B: auto-UV GPU *writes* go through daemon RPC (sweep logic stays
  byte-identical Python); then de-root the scan child if feasible.
- Stability workload (better than the CUDA shader): deferred, hook designed.
- Packaging: source builds (cargo in PKGBUILD/deb/RPM/flatpak), pip stays pure
  Python. Study of other daemons = patterns only, original code, no mentions.
- Overnight hardware verification on this box: approved (restore original
  setup after; anything off → stop, leave Python daemon running).

## 2026-07-07 — Wave A1: supervisor + socket protocol + scan child mgmt

- Implemented the JSON-lines socket server, wire protocol, supervisor, and
  Auto-UV scan child management. Files (logical code lines, excl. test modules):
  `supervisor.rs` 351, `scan.rs` 166, `api.rs` 166, `main.rs` 163, `server.rs`
  146, `paths.rs` 137, `argvspec.rs` 144, `profile/mod.rs` 119 (STUB engine),
  `logging.rs` 32 → **~1424 logical / ~1740 physical non-test lines**.
- Wire parity verified byte-for-byte against a live-driven binary: `status`,
  `start`/`stop_runtime_profile`, `stop_auto_uv_scan`, streaming
  `start_auto_uv_scan` (started/line/finished), all error strings, the peercred
  denial line, and compact `json.dumps(separators=(",",":"))`-equivalent output
  (struct field order preserved via serde derive; no `Value` reordering).
- Supervisor: one `Mutex<Supervisor>` (profile engine job / scan job / generation
  counter). Scan lifecycle spawns `python3 <program_file> --auto-uv-voltage-scan
  --json-events --auto-uv-require-final-choice <opts>` with stdout+stderr merged
  on one `pipe2(O_CLOEXEC)`, `cwd=/`; two-part stop protocol (stop-file THEN
  SIGINT); disconnect → abort-file + detached drain + kill ladder
  (30s→SIGTERM→5s→SIGKILL→5s). Last-runtime.json persisted on start (with
  `program_file` for downgrade compat), read for autostart, not cleared on stop.
  Base64-env autostart deliberately dropped per DESIGN.
- Tests: **34 unit** (argv whitelist accept/reject, `%.6g`/bool formatting vs a
  36-case Python table, effective-home matrix, stop-file bytes, last-runtime
  round-trip/malformed, request-validation error strings) + **12 integration**
  (spawn the real binary on a temp socket, stub-python scan child): status idle,
  scan framing + exact child argv, concurrent-scan refusal, stop→offer file,
  disconnect→abort file + scan end, SIGINT-ignoring kill ladder, runtime-profile
  start/stop/state-file, autostart replay, peercred denial, malformed JSON,
  byte-exact unknown method/field, unknown scan option. All green;
  `cargo fmt --check` + `cargo clippy --all-targets -- -D warnings` clean.
- Fixed a real race while implementing: the scan-start critical section now runs
  under one lock (`supervisor::begin_scan`) so two concurrent requests can't both
  pass the "already running?" check and launch two children (Python holds one
  `_ACTIVE_SCAN_LOCK` across the whole section; an earlier split-lock draft did
  not).

### Deviations from spec (deliberate, parity-preserving)

1. **Malformed-JSON error text** differs (`serde_json` message vs Python `json`
   module message); the envelope shape `{"ok":false,"error":<str>}` matches and
   the only test that pins it checks shape, not text. Reproducing CPython's
   parser strings is out of scope.
2. **`last-runtime.json` written compact** (serde) vs Python's spaced default
   `json.dumps`. The file is only ever JSON-parsed (never byte-compared), so both
   directions round-trip; not a load-bearing string.
3. **OS-error text** in `failed to clear stale Auto-UV stop request: {err}` uses
   `io::Error` text, not Python's `[Errno N] …`; the load-bearing prefix matches.
4. **Client disconnects before receiving `started`**: Python leaks the scan (its
   `finally` isn't entered at that yield); we route it through the normal
   disconnect cleanup (abort + monitor) instead of leaking. Strictly better.
5. **Test-only env knobs** added (unset in production, so no behavior change):
   `PENGUIN_BURNERD_TEST_STATE_FILE` (override the `/var/lib` state path so tests
   don't touch host state — the Rust analog of the pytest `monkeypatch` of
   `LAST_RUNTIME_STATE_PATH`) and `PENGUIN_BURNERD_TEST_TIMINGS` (shrink the kill
   ladder). Both documented in-code as test-only.

### Open questions (A1)

1. **`status` takes the supervisor lock; Python's `status_payload` is lock-free.**
   So a `status` during the scan-start critical section can briefly block while
   the engine stops + the child spawns. Harmless with the A1 stub engine (stop is
   instant); flag for A3 when `engine.stop` may take seconds (release fans/clock).
   Keep locked status for simplicity, or add a lock-free status snapshot?
2. **CLI flag: only `--socket PATH` (+ optional bare `serve`) is accepted.** The
   *current* Python unit's ExecStart uses `--daemon-api <socket>`. A4's installer
   must emit `--socket`; if the overnight hardware verify reuses the existing
   unit, it needs the flag updated (or should the binary also accept
   `--daemon-api` as an alias for drop-in reuse?).
3. **Runtime-profile `pid` = the daemon's own pid** (engine is in-process), for
   both the `start` result and the `status` active_job. Spec 08 confirms no
   caller reads this pid for behavior; verify once more against the GUI in A4.

## 2026-07-07 — Wave A2: GPU backend (NVML + hidden NVAPI)

- `src/gpu/`: narrow `GpuBackend` trait (39 methods, ~1:1 with the Python
  drivers), `NvmlBackend` (real), `MockGpu` (test-only), split
  `mod.rs`/`nvml_raw.rs`/`nvapi.rs`/`backend.rs`/`mock.rs`. Production code
  ~1580 statement-lines (excl. mock + doc comments); ~2475 raw non-test lines
  incl. heavy parity docs.
- All NVML/NVAPI struct sizes asserted at compile time against ctypes `sizeof`
  (76/40/348/88844/24/6188/16/36/9248/68/24/8). Live read-only smoke test on the
  5080 matches the Python readers exactly (voltage 805000µV, VF 132/127 points,
  identical first point, mem-offset range (-2000,+6000)).
- 20 gpu unit tests + smoke pass; full crate `cargo test` green (50 unit + 12
  integration). `rustfmt` clean; `cargo clippy` clean for `src/gpu/**` (the
  remaining crate-wide clippy findings are all in non-gpu wave-A1 files).
- Static FFI-discovery notes appended to `port-notes/12-nvapi-nvml-discovery.md`
  ("Findings from the FFI implementation").

### Open questions (A2)

1. **nvml-wrapper deliberately unused.** The crate collapses `nvmlReturn_t` into
   a typed enum, discarding the integer `rc` that the load-bearing error strings
   embed (`"… failed with NVML error {rc}"`), and lacks the VF-offset /
   min-max-VF-offset / throttle-reason symbols. So the whole NVML surface is raw
   `libloading` FFI in `nvml_raw.rs` for exact parity. `nvml-wrapper` stays a
   declared-but-unused dep (Cargo.toml is frozen). OK to drop it in a later
   Cargo.toml change, or keep for future use? (Not a clippy/deny failure.)
2. **Consolidated init error shape.** The Python code has several separate NVML
   sessions with two `nvmlInit_v2`-failure message shapes (runtime session: no
   `nvmlErrorString`; policy controller: with it). The backend consolidates to
   one init and uses the **runtime-session shape** (no error text), since that's
   the primary daemon session that gates engine start. Confirm A3 doesn't rely
   on the policy-shaped init string.
3. **Staging `#![allow(dead_code)]` in `gpu/mod.rs`.** The whole backend reads as
   dead code until A3 wires it into `main` (same artifact as A1's unused
   `logging::warn`). Allow is scoped to the gpu module with a "remove when A3
   consumes it" note. Remove during A3/A4 so real dead code re-surfaces.
4. **VF write path takes kHz.** `apply_vf_offsets_khz(&[(index, offset_khz)])`
   keeps the backend unit-faithful (hidden-NVAPI curve is kHz); the plan's
   `new_offset_mhz * 1000` conversion stays in the A3 caller (matches Python
   `apply_plan`). Confirm A3 does the ×1000 there.
5. **Defensive index guard.** VF point indices ≥255 (256-bit mask vs 255-slot
   array — never observed) are skipped rather than read out of bounds; Python
   would `IndexError`. Parity-neutral in practice.

## 2026-07-07 — Wave A3: profile engine (VF apply + fan + adaptive + telemetry)

- Replaced the A1 stub engine with the full in-process runtime (specs 02/04/05,
  §06 profile loading), driving the A2 backend behind the FROZEN facade
  (`EngineOptions`/`start`/`stop`/`is_running`/`returncode` unchanged; verified
  against how `supervisor.rs` calls them). New modules under `src/profile/`
  (non-test statement lines, comments/attrs excluded):
  `profile_store.rs` 836, `telemetry.rs` 745, `mod.rs`≈700 (engine+loop),
  `adaptive.rs` 696, `fan.rs` 544, `config.rs` 398, `apply.rs` 239, `cpu.rs` 168,
  `ceiling.rs` 166, `logfmt.rs` 144, `guard.rs` 62 → **~4.7k non-test**.
- Pipeline (load-bearing order): persistence → per-point VF freq offsets (single
  `SetControl`, engine does the MHz→kHz ×1000, A2 Q4) → power limit → memory
  offset (driver-clamped, read-back) → locked clock ceiling (range mode / snap).
  Initial power/mem failures are fatal (returncode 1); apply-plan and ceiling
  failures log-and-continue exactly where Python does. `__stock__` = the reset
  path. Loop = poll → adaptive (before fan, same iteration) → fan decision →
  VF-reset guard (drift>1MHz, cooldown max(poll,10s), top-8 non-zero samples) →
  throttled overlay publish → sleep. Exit cleanup (RAII guard on stop/error/panic
  via `catch_unwind`): release fans to auto + release the clock lock; UV curve,
  power, memory, persistence deliberately left applied.
- **Tests: 96 unit + 12 integration (A1) green; 2 ignored = read-only hardware
  smokes.** `cargo fmt --check`, `cargo clippy --all-targets -D warnings`, and
  `cargo test` all clean. Parity fixtures generated by RUNNING the Python
  originals (generating command recorded in each test):
  - overlay-state.txt bytes — byte-for-byte vs `overlay/state.py::write_overlay_state`.
  - fan decision chain over a temp/time sequence → `[40,46,42,36,36,26]` vs
    `fan_curve_runtime_rules.py`.
  - adaptive transitions (near-slow promote after 3 windows, comfort-wait,
    badly-slow jump, cpu-bound block) vs `adaptive_profile_policy.py`.
  - VF plan + `flatten_target` + tier for the spec-06 example profile and a
    legacy-alias profile (`plan`/`mem_clk_vf_offset_mhz`/nested `tail_rise_bins`)
    vs `runtime_auto_uv_profile.py::load_auto_uv_final_curve`.
  Plus unit tests: apply op-ordering (mock records the sequence), stock single
  reset, guard cooldown/drift, curve interpolation edges, env-override parsing,
  CPU sampler /proc math, TOML parse (nested multi-line curve array).
- **On-hardware verify (read-only):** the Rust `format_telemetry` is
  **byte-identical** to Python's live on the 5080 in the same instant, incl.
  `mem_vf_offset=+6000MHz`, `vf_point=…`, and the `uv` delta
  (`cargo test … profile::tests::smoke -- --ignored`). The full apply/fan/adaptive
  on-hardware pass is the A4 overnight item.

### Deviations from spec (deliberate, parity-preserving)

1. **Stock reset does a SINGLE `reset_locked_core_clocks`**, matching the engine's
   `__stock__` path `vf_curve_runtime_policy._reset_gpu_to_stock`. The task asked
   for the *double*-reset quirk citing `nvidia_runtime_defaults.py`, but that
   double reset lives in `reset_nvidia_runtime_defaults` = the `--reset-gpu-defaults`
   CLI subcommand, which is NOT routed through the daemon engine (not in the
   runtime-profile argv whitelist). Python wins → single reset. **Open question
   for review.**
2. **Latency telemetry receiver deferred.** `overlay/telemetry/receiver.py` (~1k
   LoC of socket/marker ingestion) is a separate subsystem, not in the A3 specs.
   The engine consumes an injected `LatencySnapshot` (`None` at runtime); the
   consuming logic (adaptive frametime input, overlay fps/latency stickiness) is
   complete and unit-tested. Consequence today: adaptive holds its tier during
   play and the overlay omits fps/latency while GPU/fan/temp/power keep updating.
   **Open question: schedule the receiver port so adaptive switches in-game.**
3. **Absolute `CLOCK_MONOTONIC` loop clock** (like Python `time.monotonic()`) so
   the `0.0`-init "distant past" markers behave identically: the first detected VF
   drift reapplies immediately and the startup tier is demote-eligible without a
   fabricated dwell wait. (A relative clock silently broke both.)
4. **Config parsing hand-rolled.** Cargo is frozen (no `toml`), so `config.rs` is
   a tiny TOML subset parser (sections, scalars, multi-line nested arrays) for
   `penguin_burner.toml` + `overlay.toml`. This + the ownership/passwd FFI +
   Rust's per-field/brace verbosity put the engine at ~4.7k non-test lines — over
   the ~2.2k aspiration but **slightly under the ~4.97k Python it replaces**
   (measured the same way). Parity was prioritized (rule zero).
5. **Test-only inert engine** (`PENGUIN_BURNERD_TEST_STATE_FILE` /
   `PENGUIN_BURNERD_TEST_INERT_ENGINE`, unset in production) makes `start()` idle
   without touching NVML, so `cargo test` (incl. the A1 integration tests that
   spawn the binary and start a runtime profile) never mutates the real GPU.
6. **Engine cleanup swallows `ceiling.close()` errors** (log-and-continue) so fans
   always restore on every exit path; Python's `restore_default` lets `close()`
   raise after the fan restore. Strictly more robust, same observable end state.
7. **Engine journal lines go raw to stdout** (parity with Python `runtime_debug.log`
   → `print(flush=True)` → journal), not via the daemon's timestamped `logging`
   prefix, so `common/runtime_log_lines.py` parsers see the exact line grammar.
8. **gpu module dead-code allow removed**, replaced by narrow per-item
   `#[allow(dead_code)]` (with notes) on the A2 backend-contract surface A3 does
   not consume (identity/memory/throttle reads, single-fan writes, exact-lock,
   uuid/driver strings, `VfSummary`) — retained for milestone-B write RPCs.

### A2 open questions — answered by A3

- **Q4 (×1000 in the A3 caller):** yes — `apply.rs::apply_plan` does
  `new_offset_mhz * 1000` before `apply_vf_offsets_khz` (matches Python `apply_plan`).
- **Q2 (consolidated init error shape):** the runtime-session shape is what the
  engine surfaces on `NvmlBackend::open` failure; no reliance on the policy shape.
- **pid/status semantics:** unchanged (engine in-process; `active_job.pid` = daemon pid).

## 2026-07-07 ~01:00 — understanding phase + scaffold

- 9 behavioral specs produced by parallel readers and committed to
  `port-notes/` (protocol, runtime engine, NVML/hidden-NVAPI FFI, fan+adaptive,
  telemetry contract, scan/profile persistence, install/packaging,
  callers+tests, daemon-patterns study).
- `DESIGN.md` written and locked with JP's answers.
- Crate scaffolded (`burnerd/`, builds clean): minimal deps
  (serde/serde_json/nvml-wrapper/libloading/libc/anyhow), lto+strip release
  profile, cargo-deny config. rustc 1.95 on this box; passwordless sudo
  confirmed for the overnight hardware pass.

## Live-system snapshot (2026-07-07, this box — verify against this)

- Autostart state file: `{"argv": ["--silent-fan-curve", "--adaptive-auto-uv",
  "--gpu-index", "0"], ...}` — **adaptive mode with no fixed tier is the
  primary running mode**, not an edge case. The engine's adaptive controller
  and the silent fan curve are the default path to verify.
- Unit env contract the engine must honor: `PENGUIN_BURNER_ADAPTIVE_*`
  (TARGET_FPS=60, TARGET_SLOW_WINDOWS=3, NEAR_SLOW_WINDOWS=2,
  COMFORT_WINDOWS=6, PERFORMANCE_COMFORT_WINDOWS=10, DEMOTE_DWELL_S=60,
  PERFORMANCE_DEMOTE_DWELL_S=45), `SUDO_USER=jp` (effective-home),
  `PENGUIN_BURNER_Q2RTX_USER/UID/GID`, `PENGUIN_BURNER_DAEMON_ALLOWED_UID=1000`
  (peercred gate active), `PENGUIN_BURNER_DAEMON_PROGRAM_FILE=<site-packages>/penguin_burner.py`.
- Python daemon ExecStart shape: `python3 …/penguin_burner.py --daemon-api
  /run/penguin-burnerd.sock`.
- Owner field data on stability probes: the CUDA companion has NEVER caught an
  instability; Q2RTX HAS (Vulkan device-lost, esp. performance scans) — Q2RTX
  is the core detector and must never be touched (rule 2).

## Next

- Wave A1: supervisor + wire-compatible socket protocol + scan child mgmt.
- Wave A2: GPU backend (nvml-wrapper + raw FFI + hidden NVAPI, size-asserted).
- Wave A3: profile engine + fan + adaptive + telemetry (byte-compatible files).
- Wave A4: installer/packaging integration, golden pytest parity, full suites,
  live-5080 verify, then Python engine deletion.
