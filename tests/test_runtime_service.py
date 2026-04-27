from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import runtime_service


def test_launcher_script_path_prefers_script_next_to_program(tmp_path: Path) -> None:
    program = tmp_path / "penguin_burner.py"
    launcher = tmp_path / "penguin_burner.sh"
    program.write_text("# program\n", encoding="utf-8")
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")

    assert runtime_service.launcher_script_path(program) == launcher


def test_launcher_script_path_falls_back_to_installed_data(
    tmp_path: Path,
    monkeypatch,
) -> None:
    program_dir = tmp_path / "site-packages"
    program_dir.mkdir()
    program = program_dir / "penguin_burner.py"
    data_root = tmp_path / "install-root"
    launcher = data_root / "share" / "penguin-burner" / "penguin_burner.sh"
    program.write_text("# program\n", encoding="utf-8")
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(
        runtime_service.sysconfig,
        "get_path",
        lambda name: str(data_root) if name == "data" else "",
    )

    assert runtime_service.launcher_script_path(program) == launcher


def test_systemd_unit_uses_running_python_and_program_without_launcher(
    tmp_path: Path,
    monkeypatch,
) -> None:
    program = tmp_path / "site-packages" / "penguin_burner.py"
    program.parent.mkdir()
    program.write_text("# program\n", encoding="utf-8")
    monkeypatch.setattr(runtime_service.sys, "executable", "/opt/python/bin/python")

    unit = runtime_service.build_systemd_service_unit(
        program,
        ["--auto-uv-profile", "profile-a", "--silent-fan-curve"],
    )

    assert (
        f"ExecStart=/opt/python/bin/python {program} --foreground "
        "--auto-uv-profile profile-a --silent-fan-curve"
    ) in unit
    assert "penguin_burner.sh" not in unit


def test_install_systemd_service_replaces_transient_unit_before_enabling(
    tmp_path: Path,
    monkeypatch,
) -> None:
    program = tmp_path / "penguin_burner.py"
    launcher = tmp_path / "penguin_burner.sh"
    unit_path = tmp_path / "PenguinBurner.service"
    program.write_text("# program\n", encoding="utf-8")
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
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
    monkeypatch.setattr(runtime_service, "BASH", "/bin/bash")
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
