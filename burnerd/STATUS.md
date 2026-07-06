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

## Next

- Wave A1: supervisor + wire-compatible socket protocol + scan child mgmt.
- Wave A2: GPU backend (nvml-wrapper + raw FFI + hidden NVAPI, size-asserted).
- Wave A3: profile engine + fan + adaptive + telemetry (byte-compatible files).
- Wave A4: installer/packaging integration, golden pytest parity, full suites,
  live-5080 verify, then Python engine deletion.
