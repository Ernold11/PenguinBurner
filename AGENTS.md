# PenguinBurner — agent guidance

## Privileged operations go through the root daemon, never pkexec

PenguinBurner runs a single root-owned systemd daemon (`penguin-burnerd.service`,
socket `/run/penguin-burnerd.sock`). **The whole point of that daemon is to hold
the privilege**, so anything that needs root MUST be routed through it via the
socket API — the UI/CLI send a request and the already-root daemon does the work.

- **Do NOT** wrap privileged actions in `pkexec`/`sudo` (`privileged_command`).
  Each of those pops a password prompt, which is exactly what the daemon exists to
  avoid. Even a single prompt is wrong when the daemon is running.
- **Do** add/extend a daemon API method in the Rust daemon (`burnerd/` — wire the
  request through `burnerd/src/api.rs` + the supervisor; the socket protocol is
  unchanged) and call it from the client (`runtime/daemon_client.py`); the UI
  reaches it through the `"daemonize"` runtime path
  (`runtime_profile_command("daemonize", ...)`), which talks to the socket with no
  elevation prompt.
- GPU resets, VF-curve application, power limits, fan control, "restore to stock",
  etc. are all privileged and belong in the daemon.
- The rare exceptions that genuinely cannot go through the daemon are the systemd
  unit lifecycle itself (install/uninstall the service) — those still need one
  elevation because they create the daemon. Everything the *running* daemon can do,
  the daemon does.

When adding a feature that touches the GPU or system state, first ask "can the
running root daemon do this over the socket?" — the answer is almost always yes.

## Code quality & verification — run these EVERY time before commit/push

Never commit or push without running the checks below and seeing them pass. If a
check fails, fix it (or stop and report) — do not commit red.

1. **Tests** — `python -m pytest tests/ -q`. The whole suite must pass (currently
   ~1438 tests, runs in seconds). Add/adjust tests when you change behavior; a test
   that encodes the OLD behavior must be updated deliberately, not deleted to go
   green.
2. **Types/diagnostics** — resolve new Pyright errors in the files you touched.
   Pre-existing mixin/callback noise (e.g. `ProfileActionsMixin` attribute access,
   controller callback signatures) is not yours to fix, but a genuinely new error
   in your change is.
3. **Verify end-to-end** — for any change with a runtime surface, actually drive
   the affected flow and observe behavior (the `/verify` skill), not just tests.
   Exercise the real path: e.g. a GPU/daemon change → hit it through the daemon and
   read back live NVML state.
4. **Review the diff** — run `/code-review` (or `/simplify` for quality-only) on
   nontrivial diffs before committing.
5. **Clean rebuild on reinstall** — always `rm -rf build/ *.egg-info` before
   `pip install --user --force-reinstall --no-deps .`. setuptools copies sources
   into `build/lib` and does NOT prune files deleted from the tree, so a stale
   `build/` silently re-ships removed modules (this has bitten us: a reverted file
   came back in the wheel). Verify the install afterward (grep the site-packages
   copy for the change; confirm removed files are gone from the RECORD).
6. **A running process keeps its old code.** Updating installed files does not
   hot-reload a live GUI or daemon — relaunch/restart the affected process to pick
   up new code (and remember restarting the daemon re-applies its systemd
   autostart).

Only commit/push when the user asks. Branch off `main` first if on it. End commit
messages with the required Co-Authored-By trailer.
