from __future__ import annotations

import struct
import subprocess
import threading
import time
from pathlib import Path

from overlay import shim_deploy
from overlay.launcher import _configure_dxvk_nvapi_marker_output


SHIM_BYTES = b"MZ fake nvapi64 [pb-nvapi-shim] forwarder\x00\x01\x02"
REAL_BYTES = b"MZ real dxvk-nvapi nvapi64.dll\x00\x01\x02"


def _make_artifact(tmp_path: Path) -> Path:
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    artifact = shim_dir / shim_deploy.SHIM_DLL_NAME
    artifact.write_bytes(SHIM_BYTES)
    return artifact


def _make_prefix(tmp_path: Path, *, with_nvapi: bool = True) -> Path:
    """Create a prefix; optionally seed the stock nvapi64.dll. Return data path."""
    data_path = tmp_path / "compatdata"
    system32 = data_path / "pfx" / "drive_c" / "windows" / "system32"
    system32.mkdir(parents=True)
    if with_nvapi:
        (system32 / shim_deploy.SHIM_DLL_NAME).write_bytes(REAL_BYTES)
    return data_path


def _env(tmp_path: Path, data_path: Path) -> dict[str, str]:
    return {
        shim_deploy.NVAPI_SHIM_DIR_ENV: str(tmp_path / "shim"),
        "STEAM_COMPAT_DATA_PATH": str(data_path),
    }


def _system32(data_path: Path) -> Path:
    return data_path / "pfx" / "drive_c" / "windows" / "system32"


def test_artifact_discovery_prefers_env_override(tmp_path: Path) -> None:
    artifact = _make_artifact(tmp_path)
    env = {shim_deploy.NVAPI_SHIM_DIR_ENV: str(artifact.parent)}
    assert shim_deploy.nvapi_shim_artifact(env) == artifact


def test_prefix_system32_resolves_when_present(tmp_path: Path) -> None:
    data_path = _make_prefix(tmp_path)
    assert shim_deploy.prefix_system32({"STEAM_COMPAT_DATA_PATH": str(data_path)}) == (
        _system32(data_path)
    )


def test_prefix_system32_none_without_path() -> None:
    assert shim_deploy.prefix_system32({}) is None


def test_deploy_fronts_stock_nvapi_and_parks_real(tmp_path: Path) -> None:
    _make_artifact(tmp_path)
    data_path = _make_prefix(tmp_path)
    env = _env(tmp_path, data_path)
    sys32 = _system32(data_path)

    result = shim_deploy.deploy_nvapi_shim(env)
    assert result == sys32 / shim_deploy.SHIM_DLL_NAME
    # nvapi64.dll is now the shim; the real dxvk-nvapi is parked alongside.
    assert (sys32 / shim_deploy.SHIM_DLL_NAME).read_bytes() == SHIM_BYTES
    assert (sys32 / shim_deploy.REAL_SIDECAR_NAME).read_bytes() == REAL_BYTES


def test_deploy_is_idempotent(tmp_path: Path) -> None:
    _make_artifact(tmp_path)
    data_path = _make_prefix(tmp_path)
    env = _env(tmp_path, data_path)
    sys32 = _system32(data_path)

    assert shim_deploy.deploy_nvapi_shim(env) is not None
    nvapi = sys32 / shim_deploy.SHIM_DLL_NAME
    sidecar = sys32 / shim_deploy.REAL_SIDECAR_NAME
    nvapi_mtime = nvapi.stat().st_mtime_ns
    sidecar_mtime = sidecar.stat().st_mtime_ns

    # Second deploy: shim already current, must not rewrite either file.
    assert shim_deploy.deploy_nvapi_shim(env) == nvapi
    assert nvapi.stat().st_mtime_ns == nvapi_mtime
    assert sidecar.stat().st_mtime_ns == sidecar_mtime
    assert sidecar.read_bytes() == REAL_BYTES  # real not clobbered by re-park


def test_deploy_updates_stale_shim(tmp_path: Path) -> None:
    _make_artifact(tmp_path)
    data_path = _make_prefix(tmp_path, with_nvapi=False)
    sys32 = _system32(data_path)
    # An older shim already installed, plus its parked real.
    (sys32 / shim_deploy.SHIM_DLL_NAME).write_bytes(b"old [pb-nvapi-shim] build")
    (sys32 / shim_deploy.REAL_SIDECAR_NAME).write_bytes(REAL_BYTES)
    env = _env(tmp_path, data_path)

    assert shim_deploy.deploy_nvapi_shim(env) is not None
    assert (sys32 / shim_deploy.SHIM_DLL_NAME).read_bytes() == SHIM_BYTES
    assert (sys32 / shim_deploy.REAL_SIDECAR_NAME).read_bytes() == REAL_BYTES


def test_deploy_reheals_after_resync(tmp_path: Path) -> None:
    """Proton re-sync restored the stock nvapi64.dll over our shim; re-park + reinstall."""
    _make_artifact(tmp_path)
    data_path = _make_prefix(tmp_path)  # stock nvapi64.dll present again
    sys32 = _system32(data_path)
    # A stale sidecar from a previous Proton version.
    (sys32 / shim_deploy.REAL_SIDECAR_NAME).write_bytes(b"older real")
    env = _env(tmp_path, data_path)

    assert shim_deploy.deploy_nvapi_shim(env) is not None
    assert (sys32 / shim_deploy.SHIM_DLL_NAME).read_bytes() == SHIM_BYTES
    # Sidecar refreshed to the freshly-restored real.
    assert (sys32 / shim_deploy.REAL_SIDECAR_NAME).read_bytes() == REAL_BYTES


def test_deploy_skips_when_no_prefix(tmp_path: Path) -> None:
    _make_artifact(tmp_path)
    env = {shim_deploy.NVAPI_SHIM_DIR_ENV: str(tmp_path / "shim")}
    assert shim_deploy.deploy_nvapi_shim(env) is None


def test_deploy_skips_when_prefix_has_no_nvapi(tmp_path: Path) -> None:
    _make_artifact(tmp_path)
    data_path = _make_prefix(tmp_path, with_nvapi=False)
    env = _env(tmp_path, data_path)
    assert shim_deploy.deploy_nvapi_shim(env) is None


def test_deploy_guards_shim_without_sidecar(tmp_path: Path) -> None:
    """Our shim is installed but the parked real vanished: do not pretend it works."""
    _make_artifact(tmp_path)
    data_path = _make_prefix(tmp_path, with_nvapi=False)
    sys32 = _system32(data_path)
    (sys32 / shim_deploy.SHIM_DLL_NAME).write_bytes(SHIM_BYTES)  # shim, no sidecar
    env = _env(tmp_path, data_path)
    assert shim_deploy.deploy_nvapi_shim(env) is None


def test_deploy_respects_latency_disable_env(tmp_path: Path) -> None:
    _make_artifact(tmp_path)
    data_path = _make_prefix(tmp_path)
    env = _env(tmp_path, data_path)
    env[shim_deploy.NVAPI_LATENCY_DISABLE_ENV] = "1"
    assert shim_deploy.deploy_nvapi_shim(env) is None


def test_deploy_respects_legacy_shim_disable_env(tmp_path: Path) -> None:
    _make_artifact(tmp_path)
    data_path = _make_prefix(tmp_path)
    env = _env(tmp_path, data_path)
    env[shim_deploy.NVAPI_SHIM_DISABLE_ENV] = "1"
    assert shim_deploy.deploy_nvapi_shim(env) is None


def test_launcher_prefers_shim_over_trace(tmp_path: Path) -> None:
    """When the shim deploys, neither trace nor marker-log env is set."""
    _make_artifact(tmp_path)
    data_path = _make_prefix(tmp_path)
    env = _env(tmp_path, data_path)

    _configure_dxvk_nvapi_marker_output(env)

    assert "DXVK_NVAPI_LOG_LEVEL" not in env
    assert "DXVK_NVAPI_LATENCY_MARKER_LOG" not in env
    assert (_system32(data_path) / shim_deploy.REAL_SIDECAR_NAME).is_file()


def test_launcher_no_marker_output_without_prefix(tmp_path: Path) -> None:
    """No prefix -> shim skipped -> no dxvk-nvapi trace/marker-log env set;
    in-game latency degrades to the Vulkan layer's own marker tap."""
    _make_artifact(tmp_path)
    env = {shim_deploy.NVAPI_SHIM_DIR_ENV: str(tmp_path / "shim")}

    _configure_dxvk_nvapi_marker_output(env)

    assert "DXVK_NVAPI_LOG_LEVEL" not in env
    assert "DXVK_NVAPI_LATENCY_MARKER_LOG" not in env


def test_watch_and_refront_reinstalls_after_proton_clobber(tmp_path: Path) -> None:
    """Proton copies its stock nvapi64.dll over our shim mid-launch; the watcher
    re-fronts it (one iteration here) so the game still loads the shim."""
    _make_artifact(tmp_path)
    data_path = _make_prefix(tmp_path)
    env = _env(tmp_path, data_path)
    sys32 = _system32(data_path)
    nvapi = sys32 / shim_deploy.SHIM_DLL_NAME

    assert shim_deploy.deploy_nvapi_shim(env) is not None
    assert nvapi.read_bytes() == SHIM_BYTES

    # Proton's try_copy removes our shim and drops the stock real back in place.
    nvapi.write_bytes(REAL_BYTES)

    # duration_s=0 runs exactly one re-front pass, then returns.
    shim_deploy.watch_and_refront(env, duration_s=0.0, poll_s=0.0)

    assert nvapi.read_bytes() == SHIM_BYTES
    # The parked real is still the real dxvk-nvapi (not overwritten with itself
    # in a way that loses it), so the shim still has a forward target.
    assert (sys32 / shim_deploy.REAL_SIDECAR_NAME).read_bytes() == REAL_BYTES


def test_watch_seconds_default_is_session_scoped() -> None:
    """No env cap -> None: the watcher guards the whole Proton session."""
    assert shim_deploy.watch_seconds({}) is None
    assert shim_deploy.watch_seconds(
        {shim_deploy.NVAPI_SHIM_WATCH_SECONDS_ENV: "12.5"}
    ) == 12.5
    assert shim_deploy.watch_seconds(
        {shim_deploy.NVAPI_SHIM_WATCH_SECONDS_ENV: "bogus"}
    ) is None


def test_parse_inotify_events_decodes_names() -> None:
    name = b"nvapi64.dll\0\0\0\0\0"
    event = struct.pack("iIII", 1, shim_deploy._IN_CLOSE_WRITE, 0, len(name)) + name
    dir_event = struct.pack("iIII", 1, shim_deploy._IN_IGNORED, 0, 0)
    events = shim_deploy._parse_inotify_events(event + dir_event)
    assert events == [
        (shim_deploy._IN_CLOSE_WRITE, "nvapi64.dll"),
        (shim_deploy._IN_IGNORED, ""),
    ]


def _wait_readable(fd: int, timeout_s: float) -> bool:
    import select

    return bool(select.select([fd], [], [], timeout_s)[0])


def test_notifier_reports_nvapi_rewrite(tmp_path: Path) -> None:
    """A completed write of nvapi64.dll wakes the notifier; other files do not."""
    notifier = shim_deploy._Nvapi64Notifier(tmp_path)
    try:
        (tmp_path / "unrelated.dll").write_bytes(b"x")
        assert _wait_readable(notifier.fd, 0.5)
        assert notifier.drain() is False

        (tmp_path / shim_deploy.SHIM_DLL_NAME).write_bytes(REAL_BYTES)
        assert _wait_readable(notifier.fd, 0.5)
        assert notifier.drain() is True
    finally:
        notifier.close()


def test_watch_refronts_on_rewrite_event(tmp_path: Path) -> None:
    """Session-scoped watch: the watcher re-fronts as soon as Proton's rewrite
    of nvapi64.dll completes, and ends when the session process exits."""
    _make_artifact(tmp_path)
    data_path = _make_prefix(tmp_path)
    env = _env(tmp_path, data_path)
    nvapi = _system32(data_path) / shim_deploy.SHIM_DLL_NAME

    # Stand-in for the exec'd Proton session the watcher guards.
    session = subprocess.Popen(["sleep", "30"])
    watcher = threading.Thread(
        target=shim_deploy.watch_and_refront,
        args=(env,),
        kwargs={"session_pid": session.pid, "poll_s": 0.02},  # no duration cap
    )
    watcher.start()
    try:
        deadline = time.monotonic() + 2.0
        while nvapi.read_bytes() != SHIM_BYTES and time.monotonic() < deadline:
            time.sleep(0.01)
        assert nvapi.read_bytes() == SHIM_BYTES  # initial deploy landed

        nvapi.write_bytes(REAL_BYTES)  # Proton's per-launch clobber
        deadline = time.monotonic() + 2.0
        while nvapi.read_bytes() != SHIM_BYTES and time.monotonic() < deadline:
            time.sleep(0.01)
        assert nvapi.read_bytes() == SHIM_BYTES  # re-fronted well within 2s
    finally:
        session.kill()
        session.wait()
    # Session death must end the watch promptly (pidfd wakes the select).
    watcher.join(timeout=5.0)
    assert not watcher.is_alive()


def test_watch_exits_when_session_already_dead(tmp_path: Path, monkeypatch) -> None:
    """With no duration cap, a session that is already gone when the watcher
    starts must end the watch immediately (the startup race), not run forever."""
    _make_artifact(tmp_path)
    data_path = _make_prefix(tmp_path)
    env = _env(tmp_path, data_path)

    # No pidfd support and a dead pid: exercise the liveness-poll fallback.
    monkeypatch.setattr(shim_deploy, "open_session_fd", lambda _pid: None)

    def dead(_pid: int, _sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(shim_deploy.os, "kill", dead)
    monkeypatch.setattr(shim_deploy, "_SESSION_POLL_SECONDS", 0.05)

    start = time.monotonic()
    shim_deploy.watch_and_refront(env, session_pid=999999, poll_s=0.01)
    assert time.monotonic() - start < 2.0  # returned because the session is gone


def test_session_liveness_waits_for_real_steam_child_before_quiescing(
    monkeypatch,
) -> None:
    now = 10.0
    monkeypatch.setattr(
        shim_deploy, "session_alive", lambda _pid, _fd: True
    )
    monkeypatch.setattr(
        shim_deploy,
        "_proc_cmdline",
        lambda pid: (
            "/home/jp/.local/share/Steam/ubuntu12_32/reaper "
            "SteamLaunch AppId=1"
            if pid == 100
            else f"/usr/bin/python -m overlay.shim_deploy --session-pid=100 {pid}"
        ),
    )
    monkeypatch.setattr(shim_deploy, "_session_child_pids", lambda _pid: [201, 202])

    tracker = shim_deploy.SessionLiveness(
        100,
        None,
        now_fn=lambda: now,
        startup_grace_s=15.0,
    )

    assert tracker.steam_reaper_quiesced() is False


def test_session_liveness_quiesces_after_real_steam_child_exits(monkeypatch) -> None:
    children = iter(([200, 201, 202], [201, 202]))
    monkeypatch.setattr(
        shim_deploy, "session_alive", lambda _pid, _fd: True
    )
    monkeypatch.setattr(
        shim_deploy,
        "_proc_cmdline",
        lambda pid: (
            "/home/jp/.local/share/Steam/ubuntu12_32/reaper "
            "SteamLaunch AppId=1"
            if pid == 100
            else (
                "/usr/bin/python -m overlay.shim_deploy --session-pid=100"
                if pid == 201
                else (
                    "/usr/bin/python -m overlay.telemetry.nvapi_marker_bridge "
                    "--session-pid=100"
                    if pid == 202
                    else "/home/jp/.local/share/Steam/steamapps/common/Game/game.exe"
                )
            )
        ),
    )
    monkeypatch.setattr(shim_deploy, "_session_child_pids", lambda _pid: next(children))

    tracker = shim_deploy.SessionLiveness(100, None)

    assert tracker.steam_reaper_quiesced() is False
    assert tracker.steam_reaper_quiesced() is True


def test_session_liveness_does_not_quiesce_direct_game_launch(monkeypatch) -> None:
    monkeypatch.setattr(
        shim_deploy, "session_alive", lambda _pid, _fd: True
    )
    monkeypatch.setattr(
        shim_deploy,
        "_proc_cmdline",
        lambda pid: (
            "/games/Game/game.exe"
            if pid == 100
            else "/usr/bin/python -m overlay.shim_deploy --session-pid=100"
        ),
    )
    monkeypatch.setattr(shim_deploy, "_session_child_pids", lambda _pid: [201])

    tracker = shim_deploy.SessionLiveness(
        100,
        None,
        now_fn=lambda: 100.0,
        startup_grace_s=0.0,
    )

    assert tracker.steam_reaper_quiesced() is False


def test_spawn_refront_watcher_launches_detached_watch(
    tmp_path: Path, monkeypatch
) -> None:
    _make_artifact(tmp_path)
    data_path = _make_prefix(tmp_path)
    env = _env(tmp_path, data_path)
    captured: dict = {}

    class FakePopen:
        def __init__(self, argv, **kwargs) -> None:
            captured["argv"] = argv
            captured["kwargs"] = kwargs

    monkeypatch.setattr(shim_deploy.subprocess, "Popen", FakePopen)

    assert shim_deploy.spawn_refront_watcher(env) is not None
    assert captured["argv"] == [
        shim_deploy.sys.executable,
        "-m",
        "overlay.shim_deploy",
        "--watch",
        f"--session-pid={shim_deploy.os.getpid()}",
    ]
    assert captured["kwargs"]["start_new_session"] is True
    assert captured["kwargs"]["env"] is env


def test_spawn_refront_watcher_skips_when_disabled(
    tmp_path: Path, monkeypatch
) -> None:
    _make_artifact(tmp_path)
    data_path = _make_prefix(tmp_path)
    env = _env(tmp_path, data_path)
    env[shim_deploy.NVAPI_LATENCY_DISABLE_ENV] = "1"

    def fail(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("watcher must not spawn when disabled")

    monkeypatch.setattr(shim_deploy.subprocess, "Popen", fail)
    assert shim_deploy.spawn_refront_watcher(env) is None


def test_spawn_refront_watcher_skips_without_prefix(
    tmp_path: Path, monkeypatch
) -> None:
    _make_artifact(tmp_path)
    env = {shim_deploy.NVAPI_SHIM_DIR_ENV: str(tmp_path / "shim")}  # no prefix

    def fail(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("watcher must not spawn without a prefix")

    monkeypatch.setattr(shim_deploy.subprocess, "Popen", fail)
    assert shim_deploy.spawn_refront_watcher(env) is None
