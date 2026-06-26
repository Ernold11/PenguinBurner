from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from runtime.support import runtime_service


def test_systemd_unit_uses_running_python_and_program_without_launcher(
    tmp_path: Path,
    monkeypatch,
) -> None:
    program = tmp_path / "site-packages" / "penguin_burner.py"
    program.parent.mkdir()
    program.write_text("# program\n", encoding="utf-8")
    monkeypatch.setattr(runtime_service.sys, "executable", "/opt/python/bin/python")
    monkeypatch.setenv("PENGUIN_BURNER_ADAPTIVE_TARGET_FPS", "60")

    unit = runtime_service.build_systemd_service_unit(
        program,
        ["--auto-uv-profile", "profile-a", "--silent-fan-curve"],
    )

    assert (
        f"ExecStart=/opt/python/bin/python {program} "
        "--auto-uv-profile profile-a --silent-fan-curve"
    ) in unit
    assert "Environment=PENGUIN_BURNER_ADAPTIVE_TARGET_FPS=60" in unit
    assert "penguin_burner.sh" not in unit


def test_systemd_unit_uses_adaptive_target_fps_env(
    tmp_path: Path,
    monkeypatch,
) -> None:
    program = tmp_path / "penguin_burner.py"
    program.write_text("# program\n", encoding="utf-8")
    monkeypatch.setenv("PENGUIN_BURNER_ADAPTIVE_TARGET_FPS", "50")

    unit = runtime_service.build_systemd_service_unit(program, ["--adaptive-auto-uv"])

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
    unit_path = tmp_path / "PenguinBurner.service"
    program.write_text("# program\n", encoding="utf-8")
    unit_path.write_text("old unit\n", encoding="utf-8")
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
    monkeypatch.setattr(runtime_service, "systemd_service_unit_path", lambda: unit_path)
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
        ["/bin/systemctl", "enable", "--now", "PenguinBurner.service"],
    ) in actions
    assert actions.index(
        ("checked", ["/bin/systemctl", "daemon-reload"])
    ) < actions.index(
        (
            "checked",
            ["/bin/systemctl", "enable", "--now", "PenguinBurner.service"],
        )
    )
    assert "--auto-uv-profile profile-a" in unit_path.read_text(encoding="utf-8")
    assert any("persistent service install" in message for message in logs)


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
    monkeypatch.setattr(runtime_service, "systemd_service_unit_path", lambda: legacy_unit)
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
        ["/bin/systemctl", "enable", "--now", "penguin-burnerd.service"],
    ) in actions
    assert actions.index(
        ("checked", ["/bin/systemctl", "enable", "--now", "penguin-burnerd.service"])
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
    monkeypatch.setattr(runtime_service, "systemd_service_unit_path", lambda: legacy_unit)
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
    assert any("before foreground Auto-UV scan" in message for message in logs)


def test_daemonize_sets_adaptive_target_fps_env(monkeypatch) -> None:
    calls = []
    logs = []
    monkeypatch.setenv("SUDO_USER", "jp")
    monkeypatch.setenv("PENGUIN_BURNER_ADAPTIVE_TARGET_FPS", "120")
    monkeypatch.setattr(runtime_service, "SYSTEMD_RUN", "/bin/systemd-run")
    monkeypatch.setattr(runtime_service, "systemd_is_available", lambda: True)
    monkeypatch.setattr(runtime_service.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        runtime_service,
        "clear_existing_penguin_burner_unit_for_daemonize",
        lambda **_kwargs: None,
    )

    def fake_run(args, **_kwargs):
        calls.append(list(args))
        return SimpleNamespace(returncode=0, stdout="started\n", stderr="")

    monkeypatch.setattr(runtime_service.subprocess, "run", fake_run)

    runtime_service.daemonize_with_systemd(
        "/tmp/penguin_burner.py",
        ["--adaptive-auto-uv"],
        journal_hours=4,
        log=logs.append,
    )

    command = calls[0]
    assert "--setenv" in command
    assert "PENGUIN_BURNER_ADAPTIVE_TARGET_FPS=120" in command


def test_daemonize_preserves_pkexec_desktop_user_env(monkeypatch) -> None:
    calls = []
    logs = []
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setenv("PENGUIN_BURNER_HOME", "/home/jp")
    monkeypatch.setenv("PENGUIN_BURNER_Q2RTX_USER", "jp")
    monkeypatch.setattr(runtime_service, "SYSTEMD_RUN", "/bin/systemd-run")
    monkeypatch.setattr(runtime_service, "systemd_is_available", lambda: True)
    monkeypatch.setattr(runtime_service.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        runtime_service,
        "clear_existing_penguin_burner_unit_for_daemonize",
        lambda **_kwargs: None,
    )

    def fake_run(args, **_kwargs):
        calls.append(list(args))
        return SimpleNamespace(returncode=0, stdout="started\n", stderr="")

    monkeypatch.setattr(runtime_service.subprocess, "run", fake_run)

    runtime_service.daemonize_with_systemd(
        "/tmp/penguin_burner.py",
        ["--auto-uv-profile", "850mv-2762mhz"],
        journal_hours=4,
        log=logs.append,
    )

    command = calls[0]
    assert "SUDO_USER=jp" in command
    assert "PENGUIN_BURNER_HOME=/home/jp" in command
    assert "PENGUIN_BURNER_Q2RTX_USER=jp" in command
