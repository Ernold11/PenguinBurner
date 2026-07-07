"""Golden parity tests for the Rust daemon (``burnerd/``).

Spawn the REAL built ``penguin-burnerd`` binary on a temp socket and drive it with
the REAL ``runtime.daemon_client`` functions. Driving the shipped client over the
socket IS the parity contract: whatever the GUI/CLI send, the Rust daemon must
answer byte-for-byte like the Python ``runtime/daemon_api.py`` did. These port the
socket-observable behaviors that ``tests/test_daemon_api.py`` pins.

The tests are skipped (module-level) when the binary isn't built; ``cargo build
--release`` in ``burnerd/`` makes them run.

Behaviors from the Python daemon that are INTENTIONALLY NOT ported here, by
design (documented so the omission is deliberate, not an oversight):

* **Base64-env autostart** (``PENGUIN_BURNER_DAEMON_AUTOSTART_ARGV_B64``): dropped
  in the Rust port (DESIGN.md §Supervisor / Simplifications). The only autostart
  mechanism is the ``last-runtime.json`` state file, which
  ``test_autostart_runs_the_persisted_runtime_profile`` covers. There is no unit-env
  autostart path left to test.
* **The ``*_stopped`` states** (``auto_uv_scan_stopped`` /
  ``runtime_profile_stopped``) and the runtime-profile ``returncode == 1``
  (engine-error) path. Reaching them requires the engine to exit on its own; the
  A1 stub engine only stops on command, so at the socket level a profile goes
  ``idle -> running -> idle``. Deferred to A3 (real engine).
* **Engine-side GPU behavior** (VF curve, fan loop, adaptive tiers, telemetry):
  A3, not protocol. The stub engine here just idles until stopped.
* **Peer-UID (SO_PEERCRED) denial**: the daemon writes the denial line and closes
  the connection *before* reading any request, so driving it through the writing
  client (``daemon_status``) races the close into a connection reset. It is covered
  byte-exact by the Rust integration test
  ``burnerd/tests/daemon.rs::peercred_denies_non_matching_uid``.
* **Malformed-JSON error *text***: differs (``serde_json`` vs CPython ``json``); the
  Python socket test only asserts the ``{"ok":false,"error":...}`` envelope shape,
  which ``test_malformed_json_returns_error_envelope`` also does.
* **``_command_value_text`` bool-before-int / full ``%.6g`` matrix**: exhaustively
  covered by Rust unit tests (``argvspec::tests``). Here the socket path exercises
  a representative float + int + null/empty-skip case
  (``test_scan_value_formatting_*``).
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import pytest

from runtime.daemon_client import (
    daemon_payload_request,
    daemon_request,
    daemon_status,
    daemon_stream_request,
    start_runtime_profile,
    stop_runtime_profile,
)


_REPO_ROOT = Path(__file__).resolve().parents[1]


def _find_binary() -> Path | None:
    for rel in (
        "burnerd/target/release/penguin-burnerd",
        "burnerd/target/debug/penguin-burnerd",
    ):
        candidate = _REPO_ROOT / rel
        if candidate.exists():
            return candidate
    return None


BINARY = _find_binary()

pytestmark = pytest.mark.skipif(
    BINARY is None,
    reason="penguin-burnerd binary not built (run `cargo build --release` in burnerd/)",
)


# The Auto-UV scan child stub. Same behavior contract as
# ``burnerd/tests/daemon.rs::STUB``: print two lines, then exit or loop depending on
# env the daemon inherits and forwards to the child.
_STUB = r"""import os, sys, signal, time

argv_file = os.environ.get("SCAN_STUB_ARGV_FILE")
if argv_file:
    import json
    with open(argv_file, "w") as handle:
        handle.write(json.dumps(sys.argv[1:]))

sys.stdout.write('{"event":"auto_uv_start"}\n')
sys.stdout.write('human line\n')
sys.stdout.flush()

if os.environ.get("SCAN_STUB_EXIT_AFTER_LINES") == "1":
    sys.exit(0)

if os.environ.get("SCAN_STUB_IGNORE_SIGINT") == "1":
    signal.signal(signal.SIGINT, signal.SIG_IGN)
else:
    signal.signal(signal.SIGINT, lambda *_: os._exit(0))

initial_ppid = os.getppid()
while True:
    # Exit if the daemon (our parent) died and we were reparented.
    if os.getppid() != initial_ppid:
        os._exit(0)
    time.sleep(0.05)
"""


@dataclass
class DaemonHandle:
    proc: "subprocess.Popen[bytes]"
    socket_path: Path
    home: Path
    state_file: Path
    log: BinaryIO  # open file handle for the daemon's stdout/stderr

    def stop_request_file(self) -> Path:
        return self.home / ".config" / "PenguinBurner" / "auto-uv-stop-requested"


def _wait_for_socket(handle: DaemonHandle) -> None:
    for _ in range(250):
        if handle.socket_path.exists():
            return
        if handle.proc.poll() is not None:
            log_text = _read_log(handle)
            raise AssertionError(
                f"daemon exited early (rc={handle.proc.returncode}):\n{log_text}"
            )
        time.sleep(0.02)
    raise AssertionError(f"socket was not created: {handle.socket_path}")


def _read_log(handle: DaemonHandle) -> str:
    try:
        return Path(handle.log.name).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "<no log>"


def _terminate(handle: DaemonHandle) -> None:
    proc = handle.proc
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)  # teardown = SIGTERM + wait, per spec
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    try:
        handle.log.close()
    except OSError:
        pass


@pytest.fixture
def make_daemon(tmp_path):
    """Factory: start a fresh Rust daemon with the scan stub wired up.

    The stub's behavior is selected via keyword args (mapped to ``SCAN_STUB_*``
    env the daemon inherits and forwards to the spawned child).
    """
    handles: list[DaemonHandle] = []
    counter = {"n": 0}

    def _make(
        *,
        extra_env: dict[str, str] | None = None,
        exit_after_lines: bool = False,
        ignore_sigint: bool = False,
        argv_file: Path | None = None,
        seed_state: dict | None = None,
    ) -> DaemonHandle:
        counter["n"] += 1
        base = tmp_path / f"daemon-{counter['n']}"
        base.mkdir()
        home = base / "home"
        home.mkdir()
        socket_path = base / "sock"
        state_file = base / "state.json"
        if seed_state is not None:
            state_file.write_text(json.dumps(seed_state), encoding="utf-8")
        stub = base / "scan_stub.py"
        stub.write_text(_STUB, encoding="utf-8")

        env = os.environ.copy()
        env["PENGUIN_BURNER_DAEMON_PROGRAM_FILE"] = str(stub)
        env["PENGUIN_BURNER_HOME"] = str(home)  # stop-request files land under here
        env["PENGUIN_BURNERD_TEST_STATE_FILE"] = str(state_file)
        env["PENGUIN_BURNERD_TEST_INERT_ENGINE"] = "1"  # engine idles (no GPU under test)
        env["PENGUIN_BURNERD_TEST_TIMINGS"] = "1"  # fast kill ladder
        env.pop("PENGUIN_BURNER_DAEMON_ALLOWED_UID", None)  # open the peercred gate
        env.pop("NOTIFY_SOCKET", None)  # no systemd watchdog under test
        if exit_after_lines:
            env["SCAN_STUB_EXIT_AFTER_LINES"] = "1"
        if ignore_sigint:
            env["SCAN_STUB_IGNORE_SIGINT"] = "1"
        if argv_file is not None:
            env["SCAN_STUB_ARGV_FILE"] = str(argv_file)
        if extra_env:
            env.update(extra_env)

        log = open(base / "daemon.log", "wb")
        proc = subprocess.Popen(
            [str(BINARY), "--socket", str(socket_path)],
            env=env,
            stdout=log,
            stderr=log,
        )
        handle = DaemonHandle(proc, socket_path, home, state_file, log)
        handles.append(handle)
        _wait_for_socket(handle)
        return handle

    yield _make

    for handle in handles:
        _terminate(handle)


# --- helpers -----------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_until(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _read_stop_file(path: Path, timeout: float = 4.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            time.sleep(0.02)
    raise AssertionError(f"stop-request file never appeared: {path}")


def _raw_request(socket_path: Path, payload: bytes) -> dict:
    """Send raw bytes over the socket and return the first parsed response line."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5.0)
        client.connect(str(socket_path))
        client.sendall(payload)
        chunks: list[bytes] = []
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
    line = b"".join(chunks).split(b"\n", 1)[0]
    return json.loads(line.decode("utf-8"))


# --- status ------------------------------------------------------------------


def test_status_idle_shape(make_daemon):
    daemon = make_daemon()
    status = daemon_status(socket_path=daemon.socket_path)
    assert status["state"] == "idle"
    assert status["active_job"] is None
    assert isinstance(status["version"], str) and status["version"]


# --- runtime profile: start / stop / state file ------------------------------


def test_start_runtime_profile_tracks_process_and_state_file(make_daemon):
    daemon = make_daemon()
    argv = ["--auto-uv-profile", "profile-a", "--silent-fan-curve"]

    result = start_runtime_profile(argv, socket_path=daemon.socket_path)
    # In-process engine: the "job pid" is the daemon's own pid (design change vs the
    # Python child; spec 08 confirms no caller reads this pid for behavior).
    assert result == {"started": True, "pid": daemon.proc.pid, "argv": argv}

    status = daemon_status(socket_path=daemon.socket_path)
    assert status["state"] == "runtime_profile_running"
    job = status["active_job"]
    assert job["type"] == "runtime_profile"
    assert job["pid"] == daemon.proc.pid
    assert job["returncode"] is None
    assert job["argv"] == argv

    # State file persisted with the argv round-tripped + a program_file key.
    state = json.loads(daemon.state_file.read_text(encoding="utf-8"))
    assert state["argv"] == argv
    assert isinstance(state["program_file"], str) and state["program_file"]


def test_start_runtime_profile_accepts_inline_equals_forms(make_daemon):
    daemon = make_daemon()
    argv = ["--auto-uv-profile=latest", "--gpu-index=1"]

    result = start_runtime_profile(argv, socket_path=daemon.socket_path)
    assert result["started"] is True
    assert result["argv"] == argv

    status = daemon_status(socket_path=daemon.socket_path)
    assert status["state"] == "runtime_profile_running"
    assert status["active_job"]["argv"] == argv


def test_stop_runtime_profile_returns_to_idle_but_keeps_state_file(make_daemon):
    daemon = make_daemon()
    argv = ["--auto-uv-profile", "profile-a"]
    start_runtime_profile(argv, socket_path=daemon.socket_path)

    stop = stop_runtime_profile(socket_path=daemon.socket_path)
    assert stop["stopped"] is True
    assert stop["pid"] == daemon.proc.pid

    status = daemon_status(socket_path=daemon.socket_path)
    assert status["state"] == "idle"
    # Parity: stop does NOT clear the state file (a restart re-runs the last action).
    assert daemon.state_file.exists()


def test_stop_runtime_profile_when_idle(make_daemon):
    daemon = make_daemon()
    stop = stop_runtime_profile(socket_path=daemon.socket_path)
    assert stop == {"stopped": False, "state": "idle"}


# --- runtime profile: argv whitelist rejections (byte-exact) ------------------


def test_start_runtime_profile_rejects_unsupported_arg(make_daemon):
    daemon = make_daemon()
    with pytest.raises(RuntimeError, match="^unsupported runtime profile argument: --daemon-api$"):
        start_runtime_profile(["--daemon-api", "/tmp/x"], socket_path=daemon.socket_path)


def test_start_runtime_profile_rejects_value_form_of_bare_flag(make_daemon):
    daemon = make_daemon()
    # `--silent-fan-curve=x` is neither a value flag nor an `=`-prefixed whitelist form.
    with pytest.raises(
        RuntimeError,
        match="^unsupported runtime profile argument: --silent-fan-curve=x$",
    ):
        start_runtime_profile(["--silent-fan-curve=x"], socket_path=daemon.socket_path)


def test_start_runtime_profile_requires_value_for_value_flag(make_daemon):
    daemon = make_daemon()
    with pytest.raises(RuntimeError, match="^--auto-uv-profile requires a value$"):
        # daemon_payload_request preserves the argv exactly (no client-side coercion).
        daemon_payload_request(
            {"method": "start_runtime_profile", "argv": ["--auto-uv-profile"]},
            socket_path=daemon.socket_path,
        )


def test_start_runtime_profile_rejects_non_string_list(make_daemon):
    daemon = make_daemon()
    for bad in ("notalist", [1, 2], None):
        payload: dict[str, object] = {"method": "start_runtime_profile"}
        if bad is not None:
            payload["argv"] = bad
        with pytest.raises(
            RuntimeError, match="^runtime profile argv must be a JSON string list$"
        ):
            daemon_payload_request(payload, socket_path=daemon.socket_path)


# --- request validation error strings (byte-exact) ---------------------------


def test_unknown_method(make_daemon):
    daemon = make_daemon()
    with pytest.raises(RuntimeError, match="^unknown daemon method: run_cli$"):
        daemon_payload_request({"method": "run_cli"}, socket_path=daemon.socket_path)


def test_unknown_field(make_daemon):
    daemon = make_daemon()
    with pytest.raises(RuntimeError, match="^unknown request field: argv$"):
        daemon_payload_request(
            {"method": "status", "argv": ["--x"]}, socket_path=daemon.socket_path
        )


def test_missing_or_empty_method(make_daemon):
    daemon = make_daemon()
    for payload in ({}, {"method": ""}):
        with pytest.raises(RuntimeError, match="^request method is required$"):
            daemon_payload_request(payload, socket_path=daemon.socket_path)


def test_empty_lines_are_tolerated(make_daemon):
    daemon = make_daemon()
    # Leading empty/whitespace lines are skipped (server `continue`s) before status.
    response = _raw_request(daemon.socket_path, b"\n   \n{\"method\":\"status\"}\n")
    assert response["ok"] is True
    assert response["result"]["state"] == "idle"


def test_malformed_json_returns_error_envelope(make_daemon):
    daemon = make_daemon()
    response = _raw_request(daemon.socket_path, b"{bad json\n")
    assert response["ok"] is False
    assert "error" in response  # error *text* differs from Python; shape matches


# --- Auto-UV scan streaming --------------------------------------------------


def test_scan_streams_started_line_finished(make_daemon, tmp_path):
    argv_file = tmp_path / "scan-argv.json"
    daemon = make_daemon(exit_after_lines=True, argv_file=argv_file)

    frames = list(
        daemon_stream_request(
            {
                "method": "start_auto_uv_scan",
                "options": {"gpu_index": 0, "auto_uv_mode": "performance"},
            },
            socket_path=daemon.socket_path,
        )
    )

    assert frames[0]["ok"] is True
    assert frames[0]["control"] == "started"
    assert isinstance(frames[0]["pid"], int)
    assert frames[1] == {"ok": True, "line": '{"event":"auto_uv_start"}\n'}
    assert frames[2] == {"ok": True, "line": "human line\n"}
    assert frames[3]["control"] == "finished"
    assert frames[3]["exit_code"] == 0

    # The child was launched with the exact scan command argv (fixed prefix +
    # mapped options in flag-map order).
    launched = json.loads(argv_file.read_text(encoding="utf-8"))
    assert launched == [
        "--auto-uv-voltage-scan",
        "--json-events",
        "--auto-uv-require-final-choice",
        "--gpu-index",
        "0",
        "--auto-uv-mode",
        "performance",
    ]


def test_scan_value_formatting_skips_null_empty_and_formats_float(make_daemon, tmp_path):
    argv_file = tmp_path / "scan-argv-values.json"
    daemon = make_daemon(exit_after_lines=True, argv_file=argv_file)

    frames = list(
        daemon_stream_request(
            {
                "method": "start_auto_uv_scan",
                "options": {
                    "gpu_index": 1,
                    "auto_uv_mode": None,  # None -> skipped
                    "auto_uv_min_voltage_mv": "",  # "" -> skipped
                    "auto_uv_max_clock_drop_pct": 6.5,  # float -> "6.5" (%.6g)
                    "auto_uv_power_limit_w": 300,  # int -> "300"
                },
            },
            socket_path=daemon.socket_path,
        )
    )
    assert frames[-1]["control"] == "finished"

    launched = json.loads(argv_file.read_text(encoding="utf-8"))
    assert launched == [
        "--auto-uv-voltage-scan",
        "--json-events",
        "--auto-uv-require-final-choice",
        "--gpu-index",
        "1",
        "--auto-uv-max-clock-drop-pct",
        "6.5",
        "--auto-uv-power-limit-w",
        "300",
    ]


def test_unknown_scan_option_is_rejected(make_daemon):
    daemon = make_daemon()
    frames = list(
        daemon_stream_request(
            {"method": "start_auto_uv_scan", "options": {"bogus": 1}},
            socket_path=daemon.socket_path,
        )
    )
    assert frames == [{"ok": False, "error": "unknown Auto-UV option: bogus"}]


def test_second_scan_is_refused_while_one_runs(make_daemon):
    daemon = make_daemon()  # default stub loops (long-running)

    first = daemon_stream_request(
        {"method": "start_auto_uv_scan", "options": {"gpu_index": 0}},
        socket_path=daemon.socket_path,
    )
    try:
        started = next(first)
        assert started["control"] == "started"

        # Second scan on a fresh connection is refused with the exact message.
        second = list(
            daemon_stream_request(
                {"method": "start_auto_uv_scan", "options": {"gpu_index": 0}},
                socket_path=daemon.socket_path,
            )
        )
        assert second == [{"ok": False, "error": "Auto-UV scan is already running"}]
    finally:
        first.close()


def test_stop_auto_uv_writes_offer_stop_file(make_daemon):
    daemon = make_daemon()  # default stub loops until signalled

    scan = daemon_stream_request(
        {"method": "start_auto_uv_scan", "options": {"gpu_index": 0}},
        socket_path=daemon.socket_path,
    )
    try:
        assert next(scan)["control"] == "started"

        stop = daemon_request("stop_auto_uv_scan", socket_path=daemon.socket_path)
        assert stop["stopped"] is True

        content = _read_stop_file(daemon.stop_request_file())
        assert content == (
            "stop requested by PenguinBurner daemon client\n"
            "reason=offer-final-choice\n"
        )
    finally:
        scan.close()


def test_stop_auto_uv_when_idle(make_daemon):
    daemon = make_daemon()
    stop = daemon_request("stop_auto_uv_scan", socket_path=daemon.socket_path)
    assert stop == {"stopped": False, "state": "idle"}


def test_disconnect_writes_abort_stop_file_and_ends_scan(make_daemon):
    daemon = make_daemon()  # default stub: exits on SIGINT

    scan = daemon_stream_request(
        {"method": "start_auto_uv_scan", "options": {"gpu_index": 0}},
        socket_path=daemon.socket_path,
    )
    started = next(scan)
    assert started["control"] == "started"
    scan_pid = started["pid"]

    # Disconnect mid-stream: closing the generator closes the socket.
    scan.close()

    # Daemon writes an abort stop-request and SIGINTs the child.
    content = _read_stop_file(daemon.stop_request_file())
    assert content == (
        "stop requested by PenguinBurner daemon client\nreason=abort-final-choice\n"
    )

    # The child exits and the daemon returns to idle.
    assert _wait_until(lambda: not _pid_alive(scan_pid), timeout=5.0)
    assert _wait_until(
        lambda: daemon_status(socket_path=daemon.socket_path)["state"] == "idle",
        timeout=5.0,
    )


def test_kill_ladder_terminates_a_sigint_ignoring_scan(make_daemon):
    daemon = make_daemon(ignore_sigint=True)  # child ignores SIGINT

    scan = daemon_stream_request(
        {"method": "start_auto_uv_scan", "options": {"gpu_index": 0}},
        socket_path=daemon.socket_path,
    )
    started = next(scan)
    scan_pid = started["pid"]
    assert _pid_alive(scan_pid)

    # Disconnect: SIGINT is ignored, so the ladder escalates to SIGTERM/SIGKILL.
    # Under PENGUIN_BURNERD_TEST_TIMINGS the ladder fires within ~2s.
    scan.close()

    assert _wait_until(lambda: not _pid_alive(scan_pid), timeout=8.0), (
        "kill ladder did not terminate the SIGINT-ignoring scan"
    )
    assert _wait_until(
        lambda: daemon_status(socket_path=daemon.socket_path)["state"] == "idle",
        timeout=5.0,
    )


# --- autostart from the persisted state file ---------------------------------


def test_autostart_runs_the_persisted_runtime_profile(make_daemon):
    daemon = make_daemon(
        seed_state={
            "argv": ["--auto-uv-profile", "seeded"],
            "program_file": "/does/not/matter",
        }
    )
    status = daemon_status(socket_path=daemon.socket_path)
    assert status["state"] == "runtime_profile_running"
    assert status["active_job"]["argv"] == ["--auto-uv-profile", "seeded"]
