from __future__ import annotations

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


def test_deploy_respects_disable_env(tmp_path: Path) -> None:
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
    ]
    assert captured["kwargs"]["start_new_session"] is True
    assert captured["kwargs"]["env"] is env


def test_spawn_refront_watcher_skips_when_disabled(
    tmp_path: Path, monkeypatch
) -> None:
    _make_artifact(tmp_path)
    data_path = _make_prefix(tmp_path)
    env = _env(tmp_path, data_path)
    env[shim_deploy.NVAPI_SHIM_DISABLE_ENV] = "1"

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
