# Flatpak Root Daemon Plan

## Goal

Make PenguinBurner acceptable for Flathub without a large repository reshuffle.
The Flatpak build should run the GUI as an unprivileged sandboxed app, while
privileged GPU mutations go through a small root-owned service with a bounded
API.

This branch migrates privileged operations to the daemon architecture for all
supported package formats, not only Flatpak. It removes the current model where
the GUI prefixes broad CLI commands with `pkexec` or `sudo`.

## Current State

- The GUI and CLI are already separated at the entry-point level.
- Auto-UV and runtime profile commands are currently escalated by
  `ui/commands.py` using `pkexec` or `sudo` when the process is not root. This
  is the legacy path to retire in this branch.
- Runtime profile service support already exists under `runtime/support`.
- Auto-UV, fan control, power limits, clock offsets, and V/F curve application
  reuse existing Python modules and should not be rewritten for the first
  Flatpak pass.

## Flathub Direction

Use the same broad packaging shape as LACT:

- sandboxed Flatpak GUI
- first-run setup for a host/root systemd service
- root service listens on a Unix socket under `/run`
- GUI talks to the service with structured requests
- no arbitrary root shell command or arbitrary CLI passthrough from the sandbox

Relevant precedent:

- LACT on Flathub: <https://flathub.org/apps/io.github.ilya_zlobintsev.LACT>
- LACT Flatpak manifest:
  <https://raw.githubusercontent.com/flathub/io.github.ilya_zlobintsev.LACT/master/io.github.ilya_zlobintsev.LACT.yaml>
- LACT startup script:
  <https://raw.githubusercontent.com/ilya-zlobintsev/LACT/master/flatpak/startup.sh>

## Process Split

### Flatpak GUI, Unprivileged

Keep these in the sandbox:

- PySide GUI
- dialogs, plots, profile list, profile editor
- overlay configuration UI
- profile import/export where it only touches user-owned files
- user prompts and confirmations
- display of daemon events and logs
- game overlay wrapper/layer setup

The overlay itself should not require root. The game wrapper edits environment
variables, installs Vulkan layer paths into the game environment, and uses
user-owned cache/state paths.

### Root Service, Privileged

Put these behind the daemon API:

- Auto-UV scan orchestration
- runtime profile apply/stop/status
- adaptive UV runtime loop
- V/F curve apply/reset
- power limit apply/reset
- core and memory clock offset apply/reset
- fan control
- hardware cleanup on stop/crash
- read/write operations that currently need root-owned runtime context

The service may reuse current CLI/runtime/Auto-UV modules internally. The
important boundary is external: the socket API must expose typed operations, not
"run these CLI args as root".

## Daemon API Shape

Add a small daemon entry point, for example:

```text
penguin-burner-cli --daemon-api /run/penguin-burnerd.sock
```

Initial request types:

- `status`
- `start_auto_uv_scan`
- `stop_auto_uv_scan`
- `start_runtime_profile`
- `stop_runtime_profile`
- `runtime_status`
- `apply_profile`
- `reset_gpu_policy`

Requests should be JSON objects or another simple framed protocol over the Unix
socket. Responses should stream existing JSON event payloads where possible, so
the GUI controller can reuse most of its current event handling.

Example:

```json
{
  "method": "start_auto_uv_scan",
  "gpu_index": 0,
  "auto_uv_mode": "balanced",
  "auto_uv_power_limit_w": 320,
  "auto_uv_memory_offset_mhz": 0
}
```

The daemon validates every field before dispatching to existing Auto-UV logic.

## Service Lifecycle

### First Privileged Action

The Flatpak GUI should not ask for root on first app launch. It checks for
`/run/penguin-burnerd.sock` only when the user starts an operation that needs
privileged GPU mutation.

If the socket is missing:

1. Show a clear setup prompt.
2. Explain that Auto-UV, profile application, power limits, clock offsets, and
   fan control require a root hardware service.
3. Offer to install/start the service.
4. Prompt once for the administrator password while installing/starting the
   root systemd service.
5. Use `flatpak-spawn --host` with `pkexec` or `run0` only for service setup.
6. Retry the original user action after the service is installed and reachable.

Trigger the prompt for:

- starting Auto-UV
- applying a profile now
- starting a runtime profile loop
- enabling boot/autostart profile application
- resetting GPU policy from the GUI

Do not trigger the prompt for:

- opening the GUI
- editing profiles
- configuring the overlay
- importing/exporting user-owned profile files
- viewing previous results

### Normal Use

Once installed, the service sits idle until the GUI sends a request. Starting
the GUI should not automatically apply undervolts unless the user enabled a
runtime profile/autostart behavior.

Normal Auto-UV, apply-profile, and reset operations should not prompt for the
administrator password again. They go through the already installed daemon. A
new prompt is expected only when installing, repairing, upgrading, uninstalling,
or changing boot/autostart service state.

### Existing Install Migration

Migration from the current `PenguinBurner.service` model should be lazy and
operation-triggered. The GUI may detect service state at startup for display,
but it must not ask for administrator authentication, stop services, rewrite
systemd units, or otherwise mutate the host just because the app opened.

Trigger migration on the first privileged operation after upgrade:

- starting Auto-UV
- applying a profile now
- starting a runtime or adaptive profile loop
- enabling, changing, or removing boot/autostart profile application
- resetting GPU policy
- applying fan, power, clock, or V/F curve changes

If the old `PenguinBurner.service` unit exists, the setup path should:

1. Read the old unit and running systemd state before changing anything.
2. Infer the previous runtime intent from `ExecStart` and environment values:
   selected profile, adaptive mode, silent fan curve, GPU index, and whether the
   unit was enabled for boot.
3. Prompt once for administrator authentication as part of daemon setup or
   migration.
4. Install and start `penguin-burnerd.service`.
5. Migrate the previous boot/autostart intent into the new daemon model when the
   old unit was enabled.
6. Stop and disable the old `PenguinBurner.service` only after the new daemon is
   installed and reachable.
7. Retry the original user action.

If migration fails before the new daemon is reachable, leave the old unit in
place when possible and report a clear error. Do not silently remove working old
behavior. If the old unit was partially modified, report the exact recovery
command or offer a repair action.

The old broad `pkexec`/`sudo` command path may remain temporarily for native
packages only while daemon packaging and migration are being verified. It should
not be used by the Flatpak build, and it should be removed once COPR/RPM,
Debian/PPA, and AUR all ship a root-owned daemon path and migration tests pass.

### Boot Behavior

Use one service for both interactive privileged operations and boot/autostart
profile application. Do not create a second service for autostart.

Support these explicit states:

- service installed but idle
- service enabled at boot for runtime profile autostart

Auto-UV scans remain user-started jobs. The service existing at boot does not
mean a scan starts at boot.

The daemon should own one hardware-mutating job at a time:

- idle
- Auto-UV scan running
- runtime/adaptive profile loop running
- one-shot profile apply
- one-shot GPU policy reset

If a user starts Auto-UV while a runtime loop is active, the GUI and daemon must
preserve the current behavior: snapshot the active runtime/autostart state,
stop the runtime loop for the scan duration, reset or release hardware as
needed, run Auto-UV with exclusive GPU control, then restore the previous
runtime/autostart state according to today's completion/abort semantics.

## Security Model

Asking for root every time feels conservative, but in the current Flatpak shape
it authorizes a broad root CLI process repeatedly. A daemon can be safer if the
API is narrow and validated.

Required daemon rules:

- no shell command endpoint
- no arbitrary CLI passthrough
- validate GPU index and all numeric ranges
- reject unknown request fields for privileged operations
- allow only one active hardware-mutating job at a time
- reset GPU policy on job cancellation where appropriate
- restrict socket access to the intended local user or local desktop session
- log privileged actions clearly

Optional later hardening:

- use Polkit actions per high-risk operation
- split read-only telemetry from mutating operations
- make Auto-UV stability workloads run as the desktop user while the daemon
  retains root ownership of GPU policy changes

## Minimal Repository Changes

Avoid dramatic repo structure changes.

Additive files:

- `runtime/daemon_api.py` or similar socket server module
- `runtime/daemon_client.py` or similar socket client module
- `packaging/flatpak/` manifest and wrapper scripts
- root service template for `penguin-burnerd.service`

Small edits:

- add a daemon CLI flag or script entry point
- teach GUI and CLI privileged actions to call the daemon client instead of
  wrapping root commands with `pkexec` or `sudo`

Avoid initially:

- moving package directories
- rewriting Auto-UV
- replacing the existing CLI
- leaving duplicate privileged workflows in place after the daemon path works

## Native Package Rollout

The daemon API is the common privileged workflow for all package formats in this
branch. The service installation method varies by package type, but GUI and CLI
privileged operations should all route through `penguin-burnerd` once the branch
is complete.

### RPM, Debian, PPA, AUR

Distro packages can ship a root-owned daemon executable and a systemd unit
directly. For these packages, the target behavior matches Flatpak:

- GUI starts without root.
- First privileged action prompts to start/enable `penguin-burnerd.service` if
  needed.
- The service handles Auto-UV, profile application, fan control, and reset
  operations through the same socket API.

This gives users a consistent workflow across COPR/RPM, Debian/PPA, AUR, and
Flatpak.

### PyPI or User-Local Installs

PyPI is different because `pip install --user penguin-burner` installs code
under a user-writable directory. A root systemd service should not run Python
code from a user-writable path.

For PyPI/user-local installs, privileged operations require one of these daemon
installation paths:

- PenguinBurner is installed system-wide into a root-owned location.
- A separate root-owned service installer copies only the daemon payload into a
  root-owned location.
- The user installs a distro package for the service and uses the PyPI install
  only as a client.

Do not silently create a root service that executes code from `~/.local`,
`pipx`, or another user-writable environment.

Do not keep broad `pkexec`/`sudo` fallback code for PyPI/user-local installs.
If no safe root-owned daemon path is available, report a clear error and point
the user to a supported service installation method.

## Legacy Code Cleanup

Remove broad privileged command wrapping after daemon coverage exists:

- delete or replace `pkexec`/`sudo` escalation helpers in `ui/commands.py`
- remove GUI command construction that launches privileged CLI subprocesses
- replace privileged GUI actions with daemon-client calls
- keep non-privileged CLI commands local
- route privileged CLI commands through the daemon
- remove stale docs that tell users to rerun broad commands with `sudo` where a
  daemon operation is now expected

The cleanup should happen in this branch, not as an indefinite follow-up.

## Grill-Me Review Checkpoint

Before implementation, stress-test the plan with a focused review. Ask and
resolve one question at a time, with a recommended answer for each question.

Decision areas to cover:

- service installation trigger and prompt wording
- whether boot autostart is opt-in and separate from service installation
- daemon API scope and which operations are intentionally excluded
- how the daemon authenticates the local GUI/user over the Unix socket
- how user-owned files are read/written when the daemon runs as root
- whether Q2RTX/CUDA workloads run as root or as the desktop user
- what happens if Auto-UV starts while runtime profile application is active
- how service uninstall, upgrade, and broken-service recovery work
- how PyPI/user-local installs report unsupported privileged operations when no
  root-owned daemon path exists

## Implementation Phases

### Phase 1: Daemon Skeleton

- Add Unix socket server.
- Add client helper.
- Implement `status`.
- Add tests for request validation and socket framing.

### Phase 1.5: Native Package Migration

- Add old `PenguinBurner.service` detection.
- Parse old service runtime/autostart intent.
- Add lazy first-privileged-operation migration flow.
- Keep startup non-mutating.
- Add upgrade tests for old enabled service, old running transient service,
  missing daemon, broken daemon, and failed migration.
- Gate removal of the legacy native `pkexec`/`sudo` fallback on successful
  COPR/RPM, Debian/PPA, and AUR daemon packaging.

### Phase 2: Auto-UV Through Daemon

- Add `start_auto_uv_scan` and `stop_auto_uv_scan`.
- Reuse existing Auto-UV event streaming.
- GUI and CLI privileged Auto-UV actions use the daemon client.

### Phase 3: Runtime Profile Operations

- Add runtime profile start/stop/status.
- Add boot/autostart service state handling.
- Migrate native runtime profile operations to the daemon path.

### Phase 4: Flatpak Packaging

- Add manifest and wrapper scripts.
- Add metadata fixes: app ID, desktop file, icon name, AppStream metadata.
- Bundle or explicitly disable runtime-downloaded executable components as
  needed for Flathub review.

### Phase 5: Review Hardening

- Run `flatpak-builder-lint`.
- Audit permissions.
- Document privileged service rationale.
- Test first-run service install, uninstall, and update behavior.

## Open Questions

- Should service install start it for the current session only by default, with
  boot/autostart as a separate explicit choice?
- Should Q2RTX and CUDA workers both run as the desktop user from the beginning?
- Should read-only telemetry come from the daemon, the GUI, or both depending
  on sandbox driver-library access?
