from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from runtime.support import runtime_service


FLATPAK_APP_PATH = (
    "/home/jp/.local/share/flatpak/app/io.github.jpietek.PenguinBurner/"
    "current/active/files"
)
FLATPAK_SITE_PACKAGES = f"{FLATPAK_APP_PATH}/lib/python3.13/site-packages"
FLATPAK_CLI_PROGRAM = f"{FLATPAK_SITE_PACKAGES}/penguin_burner.py"
FLATPAK_DEPLOYMENT_ID = "a" * 64
FLATPAK_DEPLOYMENT_APP_PATH = (
    "/home/jp/.local/share/flatpak/app/io.github.jpietek.PenguinBurner/"
    f"x86_64/master/{FLATPAK_DEPLOYMENT_ID}/files"
)
FLATPAK_ACTIVE_APP_PATH = (
    "/home/jp/.local/share/flatpak/app/io.github.jpietek.PenguinBurner/"
    "x86_64/master/active/files"
)
FLATPAK_ACTIVE_SITE_PACKAGES = (
    f"{FLATPAK_ACTIVE_APP_PATH}/lib/python3.13/site-packages"
)
FLATPAK_ACTIVE_CLI_PROGRAM = f"{FLATPAK_ACTIVE_SITE_PACKAGES}/penguin_burner.py"


def test_daemon_systemd_unit_uses_running_python_and_program_without_launcher(
    tmp_path: Path,
    monkeypatch,
) -> None:
    program = tmp_path / "site-packages" / "penguin_burner.py"
    program.parent.mkdir()
    program.write_text("# program\n", encoding="utf-8")
    monkeypatch.setattr(runtime_service.sys, "executable", "/opt/python/bin/python")
    monkeypatch.setenv("PENGUIN_BURNER_ADAPTIVE_TARGET_FPS", "60")

    unit = runtime_service.build_daemon_api_service_unit(
        program,
        autostart_argv=["--auto-uv-profile", "profile-a", "--silent-fan-curve"],
    )

    assert (
        f"ExecStart=/opt/python/bin/python {program} "
        "--daemon-api /run/penguin-burnerd.sock"
    ) in unit
    assert "Environment=PENGUIN_BURNER_ADAPTIVE_TARGET_FPS=60" in unit
    assert "Environment=PENGUIN_BURNER_DAEMON_AUTOSTART_ARGV_B64=" in unit
    assert "penguin_burner.sh" not in unit
    assert "SyslogIdentifier=penguin-burnerd" in unit


def test_daemon_systemd_unit_uses_adaptive_target_fps_env(
    tmp_path: Path,
    monkeypatch,
) -> None:
    program = tmp_path / "penguin_burner.py"
    program.write_text("# program\n", encoding="utf-8")
    monkeypatch.setenv("PENGUIN_BURNER_ADAPTIVE_TARGET_FPS", "50")

    unit = runtime_service.build_daemon_api_service_unit(
        program,
        autostart_argv=["--adaptive-auto-uv"],
    )

    assert "Environment=PENGUIN_BURNER_ADAPTIVE_TARGET_FPS=50" in unit


def test_daemon_api_unit_uses_daemon_socket_and_autostart_argv(
    tmp_path: Path,
    monkeypatch,
) -> None:
    program = tmp_path / "penguin_burner.py"
    program.write_text("# program\n", encoding="utf-8")
    monkeypatch.setattr(runtime_service.sys, "executable", "/opt/python/bin/python")

    unit = runtime_service.build_daemon_api_service_unit(
        program,
        socket_path="/run/penguin-burnerd.sock",
        autostart_argv=["--auto-uv-profile", "profile-a", "--silent-fan-curve"],
    )

    assert (
        f"ExecStart=/opt/python/bin/python {program} "
        "--daemon-api /run/penguin-burnerd.sock"
    ) in unit
    assert "Environment=PENGUIN_BURNER_DAEMON_PROGRAM_FILE=" in unit
    assert "Environment=PENGUIN_BURNER_DAEMON_AUTOSTART_ARGV_B64=" in unit
    assert "PenguinBurner.service" not in unit


def test_daemon_api_unit_preserves_desktop_profile_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    program = tmp_path / "penguin_burner.py"
    program.write_text("# program\n", encoding="utf-8")
    monkeypatch.setenv("SUDO_USER", "jp")
    monkeypatch.setenv("PENGUIN_BURNER_HOME", "/home/jp")
    monkeypatch.setenv("PENGUIN_BURNER_Q2RTX_UID", "1000")
    monkeypatch.setenv("PENGUIN_BURNER_Q2RTX_GID", "1000")
    monkeypatch.setenv("PENGUIN_BURNER_ADAPTIVE_TARGET_FPS", "60")

    unit = runtime_service.build_daemon_api_service_unit(
        program,
        socket_path="/run/penguin-burnerd.sock",
        autostart_argv=["--adaptive-auto-uv"],
    )

    assert "Environment=SUDO_USER=jp" in unit
    assert "Environment=PENGUIN_BURNER_HOME=/home/jp" in unit
    assert "Environment=PENGUIN_BURNER_Q2RTX_UID=1000" in unit
    assert "Environment=PENGUIN_BURNER_Q2RTX_GID=1000" in unit
    assert "Environment=PENGUIN_BURNER_ADAPTIVE_TARGET_FPS=60" in unit


def test_parse_runtime_argv_from_legacy_unit() -> None:
    unit = """
[Service]
ExecStart=/usr/bin/python3 /opt/pb/penguin_burner.py --auto-uv-profile profile-a --silent-fan-curve --gpu-index 1
"""

    argv = runtime_service.parse_runtime_argv_from_unit_text(unit)

    assert argv == [
        "--auto-uv-profile",
        "profile-a",
        "--silent-fan-curve",
        "--gpu-index",
        "1",
    ]


def test_install_systemd_service_replaces_transient_unit_before_enabling(
    tmp_path: Path,
    monkeypatch,
) -> None:
    program = tmp_path / "penguin_burner.py"
    legacy_unit = tmp_path / "PenguinBurner.service"
    daemon_unit = tmp_path / "penguin-burnerd.service"
    program.write_text("# program\n", encoding="utf-8")
    legacy_unit.write_text("old unit\n", encoding="utf-8")
    actions = []
    logs = []

    def fake_run(args, **_kwargs):
        actions.append(("run", list(args)))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_run_checked(args):
        actions.append(("checked", list(args)))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setenv("SUDO_USER", "jp")
    monkeypatch.setattr(runtime_service, "SYSTEMCTL", "/bin/systemctl")
    monkeypatch.setattr(runtime_service, "systemd_is_available", lambda: True)
    monkeypatch.setattr(runtime_service.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        runtime_service,
        "clear_last_runtime_state",
        lambda: actions.append(("clear-last-runtime", [])),
    )
    monkeypatch.setattr(
        runtime_service,
        "legacy_systemd_service_unit_path",
        lambda: legacy_unit,
    )
    monkeypatch.setattr(
        runtime_service,
        "daemon_systemd_service_unit_path",
        lambda: daemon_unit,
    )
    monkeypatch.setattr(runtime_service, "_wait_for_daemon_status", lambda *_args: None)
    monkeypatch.setattr(runtime_service.subprocess, "run", fake_run)
    monkeypatch.setattr(runtime_service, "run_checked_subprocess", fake_run_checked)

    runtime_service.install_systemd_service(
        program,
        ["--auto-uv-profile", "profile-a"],
        journal_hours=4,
        log=logs.append,
    )

    assert actions[0] == (
        "run",
        ["/bin/systemctl", "disable", "--now", "PenguinBurner.service"],
    )
    assert (
        "checked",
        ["/bin/systemctl", "enable", "penguin-burnerd.service"],
    ) in actions
    assert (
        "checked",
        ["/bin/systemctl", "restart", "penguin-burnerd.service"],
    ) in actions
    assert actions.index(
        ("checked", ["/bin/systemctl", "daemon-reload"])
    ) < actions.index(
        ("clear-last-runtime", [])
    ) < actions.index(
        (
            "checked",
            ["/bin/systemctl", "restart", "penguin-burnerd.service"],
        )
    )
    unit = daemon_unit.read_text(encoding="utf-8")
    assert "--daemon-api /run/penguin-burnerd.sock" in unit
    assert "PENGUIN_BURNER_DAEMON_AUTOSTART_ARGV_B64" in unit
    assert any("persistent service install" in message for message in logs)


def test_install_systemd_service_restarts_active_daemon_after_unit_update(
    tmp_path: Path,
    monkeypatch,
) -> None:
    program = tmp_path / "penguin_burner.py"
    legacy_unit = tmp_path / "PenguinBurner.service"
    daemon_unit = tmp_path / "penguin-burnerd.service"
    program.write_text("# program\n", encoding="utf-8")
    actions = []
    waited = []

    def fake_run(args, **_kwargs):
        actions.append(("run", list(args)))
        if list(args) == ["/bin/systemctl", "is-active", "penguin-burnerd.service"]:
            return SimpleNamespace(returncode=0, stdout="active\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_run_checked(args):
        actions.append(("checked", list(args)))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setenv("SUDO_USER", "jp")
    monkeypatch.setattr(runtime_service, "SYSTEMCTL", "/bin/systemctl")
    monkeypatch.setattr(runtime_service, "systemd_is_available", lambda: True)
    monkeypatch.setattr(runtime_service.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        runtime_service,
        "clear_last_runtime_state",
        lambda: actions.append(("clear-last-runtime", [])),
    )
    monkeypatch.setattr(
        runtime_service,
        "legacy_systemd_service_unit_path",
        lambda: legacy_unit,
    )
    monkeypatch.setattr(
        runtime_service,
        "daemon_systemd_service_unit_path",
        lambda: daemon_unit,
    )
    monkeypatch.setattr(runtime_service, "_wait_for_daemon_status", waited.append)
    monkeypatch.setattr(runtime_service.subprocess, "run", fake_run)
    monkeypatch.setattr(runtime_service, "run_checked_subprocess", fake_run_checked)

    runtime_service.install_systemd_service(
        program,
        ["--auto-uv-profile", "profile-a"],
        journal_hours=4,
        log=lambda _message: None,
    )

    assert (
        "checked",
        ["/bin/systemctl", "enable", "penguin-burnerd.service"],
    ) in actions
    assert (
        "checked",
        ["/bin/systemctl", "restart", "penguin-burnerd.service"],
    ) in actions
    assert (
        "checked",
        ["/bin/systemctl", "enable", "--now", "penguin-burnerd.service"],
    ) not in actions
    assert actions.index(
        ("checked", ["/bin/systemctl", "daemon-reload"])
    ) < actions.index(
        ("clear-last-runtime", [])
    ) < actions.index(
        ("checked", ["/bin/systemctl", "restart", "penguin-burnerd.service"])
    )
    assert waited == [runtime_service.DEFAULT_DAEMON_SOCKET]


def test_migrate_to_daemon_service_disables_legacy_after_daemon_reachable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    program = tmp_path / "penguin_burner.py"
    legacy_unit = tmp_path / "PenguinBurner.service"
    daemon_unit = tmp_path / "penguin-burnerd.service"
    program.write_text("# program\n", encoding="utf-8")
    legacy_unit.write_text(
        "ExecStart=/usr/bin/python3 "
        f"{program} --auto-uv-profile profile-a --adaptive-auto-uv\n",
        encoding="utf-8",
    )
    actions = []
    logs = []

    def fake_run(args, **_kwargs):
        actions.append(("run", list(args)))
        if list(args) == ["/bin/systemctl", "is-enabled", "PenguinBurner.service"]:
            return SimpleNamespace(returncode=0, stdout="enabled\n", stderr="")
        if list(args) == ["/bin/systemctl", "is-active", "PenguinBurner.service"]:
            return SimpleNamespace(returncode=0, stdout="active\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_run_checked(args):
        actions.append(("checked", list(args)))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(runtime_service, "SYSTEMCTL", "/bin/systemctl")
    monkeypatch.setattr(runtime_service, "systemd_is_available", lambda: True)
    monkeypatch.setattr(runtime_service.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        runtime_service,
        "clear_last_runtime_state",
        lambda: actions.append(("clear-last-runtime", [])),
    )
    monkeypatch.setattr(
        runtime_service,
        "legacy_systemd_service_unit_path",
        lambda: legacy_unit,
    )
    monkeypatch.setattr(
        runtime_service,
        "daemon_systemd_service_unit_path",
        lambda: daemon_unit,
    )
    monkeypatch.setattr(runtime_service, "daemon_status", lambda **_kwargs: {"state": "idle"})
    monkeypatch.setattr(runtime_service.subprocess, "run", fake_run)
    monkeypatch.setattr(runtime_service, "run_checked_subprocess", fake_run_checked)

    runtime_service.migrate_to_daemon_service(
        program,
        socket_path="/tmp/penguin-burnerd.sock",
        log=logs.append,
    )

    assert daemon_unit.is_file()
    assert "PENGUIN_BURNER_DAEMON_AUTOSTART_ARGV_B64" in daemon_unit.read_text(
        encoding="utf-8"
    )
    assert (
        "checked",
        ["/bin/systemctl", "enable", "penguin-burnerd.service"],
    ) in actions
    assert (
        "checked",
        ["/bin/systemctl", "restart", "penguin-burnerd.service"],
    ) in actions
    assert actions.index(
        ("clear-last-runtime", [])
    ) < actions.index(
        ("checked", ["/bin/systemctl", "restart", "penguin-burnerd.service"])
    ) < actions.index(
        ("run", ["/bin/systemctl", "disable", "--now", "PenguinBurner.service"])
    )
    assert any("Migrated enabled PenguinBurner.service" in message for message in logs)


def test_migrate_to_daemon_service_refuses_unparsed_enabled_legacy_unit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    program = tmp_path / "penguin_burner.py"
    legacy_unit = tmp_path / "PenguinBurner.service"
    daemon_unit = tmp_path / "penguin-burnerd.service"
    program.write_text("# program\n", encoding="utf-8")
    legacy_unit.write_text("ExecStart=/usr/bin/other-tool\n", encoding="utf-8")

    def fake_run(args, **_kwargs):
        if list(args) == ["/bin/systemctl", "is-enabled", "PenguinBurner.service"]:
            return SimpleNamespace(returncode=0, stdout="enabled\n", stderr="")
        if list(args) == ["/bin/systemctl", "is-active", "PenguinBurner.service"]:
            return SimpleNamespace(returncode=0, stdout="active\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(runtime_service, "SYSTEMCTL", "/bin/systemctl")
    monkeypatch.setattr(runtime_service, "systemd_is_available", lambda: True)
    monkeypatch.setattr(runtime_service.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        runtime_service,
        "legacy_systemd_service_unit_path",
        lambda: legacy_unit,
    )
    monkeypatch.setattr(
        runtime_service,
        "daemon_systemd_service_unit_path",
        lambda: daemon_unit,
    )
    monkeypatch.setattr(runtime_service.subprocess, "run", fake_run)

    try:
        runtime_service.migrate_to_daemon_service(
            program,
            socket_path="/tmp/penguin-burnerd.sock",
            log=lambda _message: None,
        )
    except RuntimeError as exc:
        assert "could not be parsed" in str(exc)
    else:
        raise AssertionError("expected migration failure")

    assert not daemon_unit.exists()


def test_stop_existing_runtime_does_not_disable_persistent_service(monkeypatch) -> None:
    calls = []
    logs = []

    def fake_run(args, **_kwargs):
        calls.append(list(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(runtime_service, "SYSTEMCTL", "/bin/systemctl")
    monkeypatch.setattr(runtime_service, "systemd_is_available", lambda: True)
    monkeypatch.setattr(runtime_service.os, "geteuid", lambda: 0)
    monkeypatch.setattr(runtime_service.subprocess, "run", fake_run)

    runtime_service.stop_existing_penguin_burner_runtime(log=logs.append)

    assert ["/bin/systemctl", "stop", "PenguinBurner.service"] in calls
    assert ["/bin/systemctl", "disable", "--now", "PenguinBurner.service"] not in calls
    assert ["/bin/systemctl", "stop", "penguin-burnerd.service"] not in calls
    assert ["/bin/systemctl", "disable", "--now", "penguin-burnerd.service"] not in calls
    assert any("before foreground Auto-UV scan" in message for message in logs)


def test_daemonize_starts_daemon_service_and_runtime_profile(tmp_path, monkeypatch) -> None:
    calls = []
    logs = []
    starts = []
    daemon_unit = tmp_path / "penguin-burnerd.service"
    legacy_unit = tmp_path / "PenguinBurner.service"
    monkeypatch.setenv("SUDO_USER", "jp")
    monkeypatch.setenv("PENGUIN_BURNER_ADAPTIVE_TARGET_FPS", "120")
    monkeypatch.setattr(runtime_service, "SYSTEMCTL", "/bin/systemctl")
    monkeypatch.setattr(runtime_service, "systemd_is_available", lambda: True)
    monkeypatch.setattr(runtime_service.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        runtime_service,
        "daemon_systemd_service_unit_path",
        lambda: daemon_unit,
    )
    monkeypatch.setattr(
        runtime_service,
        "legacy_systemd_service_unit_path",
        lambda: legacy_unit,
    )
    monkeypatch.setattr(runtime_service, "_wait_for_daemon_status", lambda *_args: None)
    monkeypatch.setattr(
        runtime_service,
        "start_runtime_profile",
        lambda argv, **_kwargs: starts.append(list(argv)) or {"pid": 4321},
    )

    def fake_run(args, **_kwargs):
        calls.append(list(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_run_checked(args):
        calls.append(list(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(runtime_service.subprocess, "run", fake_run)
    monkeypatch.setattr(runtime_service, "run_checked_subprocess", fake_run_checked)

    runtime_service.daemonize_with_systemd(
        "/tmp/penguin_burner.py",
        ["--adaptive-auto-uv"],
        journal_hours=4,
        log=logs.append,
    )

    assert ["/bin/systemctl", "enable", "penguin-burnerd.service"] in calls
    assert ["/bin/systemctl", "restart", "penguin-burnerd.service"] in calls
    assert ["/bin/systemctl", "start", "penguin-burnerd.service"] not in calls
    assert not any("systemd-run" in " ".join(call) for call in calls)
    assert starts == [["--adaptive-auto-uv"]]
    unit = daemon_unit.read_text(encoding="utf-8")
    assert "Environment=PENGUIN_BURNER_ADAPTIVE_TARGET_FPS=120" in unit
    assert "Started runtime profile through penguin-burnerd.service" in "\n".join(logs)


def test_daemonize_preserves_pkexec_desktop_user_env(tmp_path, monkeypatch) -> None:
    calls = []
    logs = []
    daemon_unit = tmp_path / "penguin-burnerd.service"
    legacy_unit = tmp_path / "PenguinBurner.service"
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setenv("PENGUIN_BURNER_HOME", "/home/jp")
    monkeypatch.setenv("PENGUIN_BURNER_Q2RTX_USER", "jp")
    monkeypatch.setattr(runtime_service, "SYSTEMCTL", "/bin/systemctl")
    monkeypatch.setattr(runtime_service, "systemd_is_available", lambda: True)
    monkeypatch.setattr(runtime_service.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        runtime_service,
        "daemon_systemd_service_unit_path",
        lambda: daemon_unit,
    )
    monkeypatch.setattr(
        runtime_service,
        "legacy_systemd_service_unit_path",
        lambda: legacy_unit,
    )
    monkeypatch.setattr(runtime_service, "_wait_for_daemon_status", lambda *_args: None)
    monkeypatch.setattr(
        runtime_service,
        "start_runtime_profile",
        lambda argv, **_kwargs: {"pid": 4321},
    )

    def fake_run(args, **_kwargs):
        calls.append(list(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_run_checked(args):
        calls.append(list(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(runtime_service.subprocess, "run", fake_run)
    monkeypatch.setattr(runtime_service, "run_checked_subprocess", fake_run_checked)

    runtime_service.daemonize_with_systemd(
        "/tmp/penguin_burner.py",
        ["--auto-uv-profile", "850mv-2762mhz"],
        journal_hours=4,
        log=logs.append,
    )

    unit = daemon_unit.read_text(encoding="utf-8")
    assert "Environment=SUDO_USER=jp" in unit
    assert "Environment=PENGUIN_BURNER_HOME=/home/jp" in unit
    assert "Environment=PENGUIN_BURNER_Q2RTX_USER=jp" in unit


def test_flatpak_daemon_unit_uses_host_python_deployment(monkeypatch) -> None:
    monkeypatch.setenv("FLATPAK_ID", "io.github.jpietek.PenguinBurner")
    monkeypatch.setenv("PENGUIN_BURNER_FLATPAK_APP_PATH", FLATPAK_APP_PATH)
    monkeypatch.setenv("PENGUIN_BURNER_Q2RTX_USER", "jp")
    monkeypatch.setenv("PENGUIN_BURNER_Q2RTX_UID", "1000")
    monkeypatch.setenv("PENGUIN_BURNER_Q2RTX_GID", "1000")
    monkeypatch.setattr(
        runtime_service.pwd,
        "getpwnam",
        lambda user: SimpleNamespace(pw_dir=f"/home/{user}"),
    )

    unit = runtime_service.build_daemon_api_service_unit(
        "/app/lib/python3.13/site-packages/penguin_burner.py",
        autostart_argv=["--silent-fan-curve", "--adaptive-auto-uv", "--gpu-index", "0"],
    )

    assert "Environment=HOME=/home/jp" in unit
    assert "Environment=XDG_DATA_HOME=/home/jp/.local/share" in unit
    assert "Environment=PYTHONNOUSERSITE=1" in unit
    assert "Environment=PYTHONDONTWRITEBYTECODE=1" in unit
    assert f"Environment=PYTHONPATH={FLATPAK_SITE_PACKAGES}" in unit
    assert "/app/lib/python3.13/site-packages/penguin_burner.py" not in unit
    assert "/usr/bin/flatpak" not in unit
    assert (
        f"ExecStart=/usr/bin/python3 {FLATPAK_CLI_PROGRAM} --daemon-api "
        "/run/penguin-burnerd.sock"
    ) in unit
    assert "Environment=PENGUIN_BURNER_DAEMON_AUTOSTART_ARGV_B64=" in unit


def test_flatpak_daemon_unit_execstart_uses_host_python_deployment(monkeypatch) -> None:
    monkeypatch.setenv("FLATPAK_ID", "io.github.jpietek.PenguinBurner")
    monkeypatch.setenv("PENGUIN_BURNER_FLATPAK_APP_PATH", FLATPAK_APP_PATH)
    monkeypatch.setenv("PENGUIN_BURNER_Q2RTX_USER", "jp")
    monkeypatch.setenv("PENGUIN_BURNER_Q2RTX_UID", "1000")
    monkeypatch.setenv("PENGUIN_BURNER_Q2RTX_GID", "1000")
    monkeypatch.setattr(
        runtime_service.pwd,
        "getpwnam",
        lambda user: SimpleNamespace(pw_dir=f"/home/{user}"),
    )

    unit = runtime_service.build_daemon_api_service_unit(
        "/app/lib/python3.13/site-packages/penguin_burner.py",
        autostart_argv=["--adaptive-auto-uv"],
    )

    assert "Environment=HOME=/home/jp" in unit
    assert "Environment=XDG_DATA_HOME=/home/jp/.local/share" in unit
    assert "Environment=PYTHONNOUSERSITE=1" in unit
    assert "Environment=PYTHONDONTWRITEBYTECODE=1" in unit
    assert f"Environment=PYTHONPATH={FLATPAK_SITE_PACKAGES}" in unit
    assert (
        "Environment=PENGUIN_BURNER_DAEMON_PROGRAM_FILE="
        f"{FLATPAK_CLI_PROGRAM}"
    ) in unit
    assert "/usr/bin/flatpak" not in unit
    assert (
        f"ExecStart=/usr/bin/python3 {FLATPAK_CLI_PROGRAM} --daemon-api "
        "/run/penguin-burnerd.sock"
    ) in unit


def test_flatpak_daemon_unit_uses_stable_active_deployment_path(monkeypatch) -> None:
    monkeypatch.setenv("FLATPAK_ID", "io.github.jpietek.PenguinBurner")
    monkeypatch.setenv("PENGUIN_BURNER_FLATPAK_APP_PATH", FLATPAK_DEPLOYMENT_APP_PATH)
    monkeypatch.setenv("PENGUIN_BURNER_Q2RTX_USER", "jp")
    monkeypatch.setenv("PENGUIN_BURNER_Q2RTX_UID", "1000")
    monkeypatch.setenv("PENGUIN_BURNER_Q2RTX_GID", "1000")
    monkeypatch.setattr(
        runtime_service.pwd,
        "getpwnam",
        lambda user: SimpleNamespace(pw_dir=f"/home/{user}"),
    )

    unit = runtime_service.build_daemon_api_service_unit(
        "/app/lib/python3.13/site-packages/penguin_burner.py",
    )

    assert FLATPAK_DEPLOYMENT_ID not in unit
    assert f"Environment=PYTHONPATH={FLATPAK_ACTIVE_SITE_PACKAGES}" in unit
    assert (
        f"ExecStart=/usr/bin/python3 {FLATPAK_ACTIVE_CLI_PROGRAM} "
        "--daemon-api /run/penguin-burnerd.sock"
    ) in unit


def test_flatpak_site_packages_falls_back_to_local_app_relative_path(
    monkeypatch,
) -> None:
    host_app_path = Path("/home/jp/.local/share/flatpak/app/appid/current/active/files")
    monkeypatch.setenv("FLATPAK_ID", "io.github.jpietek.PenguinBurner")
    monkeypatch.setenv("PENGUIN_BURNER_FLATPAK_APP_PATH", str(host_app_path))
    monkeypatch.delenv("PENGUIN_BURNER_FLATPAK_SITE_PACKAGES", raising=False)
    monkeypatch.setattr(
        runtime_service,
        "_flatpak_local_site_packages_relative_path",
        lambda: Path("lib/python3.13/site-packages"),
    )

    assert (
        runtime_service.flatpak_host_site_packages_path()
        == host_app_path / "lib/python3.13/site-packages"
    )
