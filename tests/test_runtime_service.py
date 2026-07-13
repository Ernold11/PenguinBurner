from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from runtime.support import runtime_service


# The native unit's ExecStart now runs the compiled Rust daemon. Tests inject a
# fixed binary path (the packaged /usr/libexec location) since the real binary
# isn't present under a tmp program dir.
RUST_DAEMON_BINARY = "/usr/libexec/penguin-burnerd"


FLATPAK_APP_PATH = (
    "/home/jp/.local/share/flatpak/app/io.github.jpietek.PenguinBurner/"
    "current/active/files"
)
FLATPAK_SITE_PACKAGES = f"{FLATPAK_APP_PATH}/lib/python3.13/site-packages"
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


@pytest.fixture(autouse=True)
def _no_live_daemon_runtime_calls(monkeypatch):
    monkeypatch.setattr(
        runtime_service,
        "_stop_active_runtime_before_daemon_restart",
        lambda: None,
    )
    monkeypatch.setattr(
        runtime_service,
        "_apply_runtime",
        lambda _argv, **_kwargs: ({"pid": 4321}, {"format_version": 1}),
    )
    monkeypatch.setattr(
        runtime_service,
        "_apply_persistent_runtime",
        lambda _argv, **_kwargs: {"pid": 4321},
    )
    monkeypatch.setattr(runtime_service, "clear_boot_runtime_spec", lambda **_kwargs: {})


def _write_fake_elf(path: Path, payload: bytes = b"daemon") -> None:
    path.write_bytes(b"\x7fELF" + payload)


def test_daemon_systemd_unit_uses_rust_binary_and_program_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    program = tmp_path / "site-packages" / "penguin_burner.py"
    program.parent.mkdir()
    program.write_text("# program\n", encoding="utf-8")
    monkeypatch.setenv("PENGUIN_BURNER_ADAPTIVE_TARGET_FPS", "60")

    # Native unit runs the Rust daemon binary with --socket (no launcher, no
    # python --daemon-api). Autostart no longer travels as a unit env.
    unit = runtime_service.build_daemon_api_service_unit(
        program,
        binary_path=RUST_DAEMON_BINARY,
    )

    assert (
        "ExecStart=/usr/libexec/penguin-burnerd "
        "--socket /run/penguin-burnerd.sock"
    ) in unit
    assert "Type=notify" in unit
    assert "WatchdogSec=30" in unit
    assert "Restart=on-failure" in unit
    assert "RestartSec=2" in unit
    # PENGUIN_BURNER_DAEMON_PROGRAM_FILE points at the Python CLI (scan children).
    assert (
        f"Environment=PENGUIN_BURNER_DAEMON_PROGRAM_FILE={program.resolve()}" in unit
    )
    assert "PENGUIN_BURNER_ADAPTIVE_TARGET_FPS" not in unit
    assert "PENGUIN_BURNER_DAEMON_AUTOSTART_ARGV_B64" not in unit
    assert "--daemon-api" not in unit
    assert "penguin_burner.sh" not in unit
    assert "SyslogIdentifier=penguin-burnerd" in unit


def test_daemon_unit_never_executes_packaged_or_dev_binary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    program = tmp_path / "checkout" / "penguin_burner.py"
    packaged = tmp_path / "user-site" / "penguin-burnerd"
    program.parent.mkdir()
    packaged.parent.mkdir()
    program.write_text("# program\n", encoding="utf-8")
    _write_fake_elf(packaged)
    monkeypatch.setattr(
        runtime_service, "_packaged_daemon_binary", lambda: packaged
    )

    unit = runtime_service.build_daemon_api_service_unit(program)

    assert "ExecStart=/usr/libexec/penguin-burnerd " in unit
    assert str(packaged) not in unit
    with pytest.raises(RuntimeError, match="must be installed at"):
        runtime_service.build_daemon_api_service_unit(
            program,
            binary_path=packaged,
        )


def test_daemon_install_source_prefers_current_payload_over_installed_copy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    packaged = tmp_path / "packaged" / "penguin-burnerd"
    dev = tmp_path / "dev" / "penguin-burnerd"
    installed = tmp_path / "libexec" / "penguin-burnerd"
    for path in (packaged, dev, installed):
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_fake_elf(path, path.parent.name.encode("ascii"))
    monkeypatch.setattr(runtime_service, "_packaged_daemon_binary", lambda: packaged)
    monkeypatch.setattr(runtime_service, "_dev_daemon_binary", lambda _program: dev)
    monkeypatch.setattr(runtime_service, "LIBEXEC_DAEMON_BINARY", installed)

    assert runtime_service.daemon_install_source_path("program.py") == packaged
    packaged.unlink()
    assert runtime_service.daemon_install_source_path("program.py") == dev
    dev.unlink()
    assert runtime_service.daemon_install_source_path("program.py") == installed


def test_atomic_daemon_install_updates_bytes_and_normalizes_mode(tmp_path: Path) -> None:
    source = tmp_path / "source" / "penguin-burnerd"
    destination = tmp_path / "libexec" / "penguin-burnerd"
    source.parent.mkdir()
    _write_fake_elf(source, b"new daemon")
    owner = os.geteuid()
    group = os.getegid()

    assert runtime_service._atomic_install_daemon_binary(
        source,
        destination,
        owner_uid=owner,
        owner_gid=group,
    )
    assert destination.read_bytes() == source.read_bytes()
    assert stat.S_IMODE(destination.stat().st_mode) == 0o755
    assert not runtime_service._atomic_install_daemon_binary(
        source,
        destination,
        owner_uid=owner,
        owner_gid=group,
    )

    destination.chmod(0o775)
    assert runtime_service._atomic_install_daemon_binary(
        source,
        destination,
        owner_uid=owner,
        owner_gid=group,
    )
    assert stat.S_IMODE(destination.stat().st_mode) == 0o755
    assert not list(destination.parent.glob(".penguin-burnerd.*"))


def test_existing_daemon_fallback_rejects_writable_mode(tmp_path: Path) -> None:
    installed = tmp_path / "libexec" / "penguin-burnerd"
    installed.parent.mkdir()
    _write_fake_elf(installed)
    installed.chmod(0o775)

    with pytest.raises(RuntimeError, match="safe ELF executable"):
        runtime_service._atomic_install_daemon_binary(
            installed,
            installed,
            owner_uid=os.geteuid(),
            owner_gid=os.getegid(),
        )

    installed.chmod(0o750)
    assert not runtime_service._atomic_install_daemon_binary(
        installed,
        installed,
        owner_uid=os.geteuid(),
        owner_gid=os.getegid(),
    )


def test_atomic_daemon_install_replaces_symlink_without_touching_target(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source" / "penguin-burnerd"
    victim = tmp_path / "victim"
    destination = tmp_path / "libexec" / "penguin-burnerd"
    source.parent.mkdir()
    destination.parent.mkdir()
    _write_fake_elf(source, b"same bytes")
    victim.write_bytes(source.read_bytes())
    destination.symlink_to(victim)

    assert runtime_service._atomic_install_daemon_binary(
        source,
        destination,
        owner_uid=os.geteuid(),
        owner_gid=os.getegid(),
    )
    assert victim.read_bytes() == source.read_bytes()
    assert not destination.is_symlink()
    assert destination.read_bytes() == source.read_bytes()


def test_atomic_daemon_install_rejects_source_symlink(tmp_path: Path) -> None:
    real_source = tmp_path / "real-source"
    source = tmp_path / "source-link"
    destination = tmp_path / "libexec" / "penguin-burnerd"
    destination.parent.mkdir()
    _write_fake_elf(real_source, b"new")
    _write_fake_elf(destination, b"old")
    destination.chmod(0o755)
    source.symlink_to(real_source)
    old_bytes = destination.read_bytes()

    with pytest.raises(RuntimeError, match="cannot safely open"):
        runtime_service._atomic_install_daemon_binary(
            source,
            destination,
            owner_uid=os.geteuid(),
            owner_gid=os.getegid(),
        )

    assert destination.read_bytes() == old_bytes


def test_atomic_daemon_install_failure_keeps_old_binary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source" / "penguin-burnerd"
    destination = tmp_path / "libexec" / "penguin-burnerd"
    source.parent.mkdir()
    destination.parent.mkdir()
    _write_fake_elf(source, b"new")
    _write_fake_elf(destination, b"old")
    destination.chmod(0o755)
    old_bytes = destination.read_bytes()

    def fail_before_replace(_descriptor: int) -> None:
        raise OSError("injected copy failure")

    monkeypatch.setattr(runtime_service.os, "fsync", fail_before_replace)
    with pytest.raises(RuntimeError, match="injected copy failure"):
        runtime_service._atomic_install_daemon_binary(
            source,
            destination,
            owner_uid=os.geteuid(),
            owner_gid=os.getegid(),
        )

    assert destination.read_bytes() == old_bytes
    assert not list(destination.parent.glob(".penguin-burnerd.*"))


def test_daemon_systemd_unit_does_not_freeze_adaptive_policy_in_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    program = tmp_path / "penguin_burner.py"
    program.write_text("# program\n", encoding="utf-8")
    monkeypatch.setenv("PENGUIN_BURNER_ADAPTIVE_TARGET_FPS", "50")

    unit = runtime_service.build_daemon_api_service_unit(
        program,
        binary_path=RUST_DAEMON_BINARY,
    )

    assert "PENGUIN_BURNER_ADAPTIVE_TARGET_FPS" not in unit


def test_daemon_api_unit_uses_daemon_socket_and_program_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    program = tmp_path / "penguin_burner.py"
    program.write_text("# program\n", encoding="utf-8")

    unit = runtime_service.build_daemon_api_service_unit(
        program,
        socket_path="/run/penguin-burnerd.sock",
        binary_path=RUST_DAEMON_BINARY,
    )

    assert (
        "ExecStart=/usr/libexec/penguin-burnerd "
        "--socket /run/penguin-burnerd.sock"
    ) in unit
    assert (
        f"Environment=PENGUIN_BURNER_DAEMON_PROGRAM_FILE={program.resolve()}" in unit
    )
    # Boot persistence is a typed spec sent over the socket, not a unit env.
    assert "PENGUIN_BURNER_DAEMON_AUTOSTART_ARGV_B64" not in unit
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
        binary_path=RUST_DAEMON_BINARY,
    )

    assert "Environment=SUDO_USER=jp" in unit
    assert "Environment=PENGUIN_BURNER_HOME=/home/jp" in unit
    assert "Environment=PENGUIN_BURNER_Q2RTX_UID=1000" in unit
    assert "Environment=PENGUIN_BURNER_Q2RTX_GID=1000" in unit
    assert "PENGUIN_BURNER_ADAPTIVE_TARGET_FPS" not in unit


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


@pytest.mark.parametrize("operation", ["install", "migrate", "daemonize"])
def test_daemon_binary_install_failure_precedes_service_mutation(
    operation: str,
    tmp_path: Path,
    monkeypatch,
) -> None:
    program = tmp_path / "penguin_burner.py"
    legacy_unit = tmp_path / "PenguinBurner.service"
    daemon_unit = tmp_path / "penguin-burnerd.service"
    state_file = tmp_path / "last-runtime.json"
    program.write_text("# program\n", encoding="utf-8")
    legacy_unit.write_text("legacy marker\n", encoding="utf-8")
    daemon_unit.write_text("daemon marker\n", encoding="utf-8")
    state_file.write_text("state marker\n", encoding="utf-8")
    monkeypatch.setattr(runtime_service, "systemd_is_available", lambda: True)
    monkeypatch.setattr(runtime_service.os, "geteuid", lambda: 0)
    monkeypatch.setattr(runtime_service, "LAST_RUNTIME_STATE_PATH", state_file)
    monkeypatch.setattr(
        runtime_service, "legacy_systemd_service_unit_path", lambda: legacy_unit
    )
    monkeypatch.setattr(
        runtime_service, "daemon_systemd_service_unit_path", lambda: daemon_unit
    )
    monkeypatch.setattr(
        runtime_service,
        "read_legacy_service_state",
        lambda: {
            "exists": True,
            "enabled": True,
            "active": True,
            "runtime_argv": ["--auto-uv-profile", "profile-a"],
        },
    )

    def fail_install(*_args, **_kwargs):
        raise RuntimeError("injected daemon copy failure")

    monkeypatch.setattr(runtime_service, "install_daemon_binary", fail_install)
    monkeypatch.setattr(
        runtime_service.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("systemctl ran after copy failure"),
    )
    monkeypatch.setattr(
        runtime_service,
        "run_checked_subprocess",
        lambda *_args, **_kwargs: pytest.fail("systemctl ran after copy failure"),
    )

    with pytest.raises(RuntimeError, match="injected daemon copy failure"):
        if operation == "install":
            runtime_service.install_systemd_service(
                program,
                ["--auto-uv-profile", "profile-a"],
                journal_hours=4,
                log=lambda _message: None,
            )
        elif operation == "migrate":
            runtime_service.migrate_to_daemon_service(
                program,
                log=lambda _message: None,
            )
        else:
            runtime_service.daemonize_with_systemd(
                program,
                ["--auto-uv-profile", "profile-a"],
                journal_hours=4,
                log=lambda _message: None,
            )

    assert legacy_unit.read_text(encoding="utf-8") == "legacy marker\n"
    assert daemon_unit.read_text(encoding="utf-8") == "daemon marker\n"
    assert state_file.read_text(encoding="utf-8") == "state marker\n"


def test_install_systemd_service_replaces_transient_unit_before_enabling(
    tmp_path: Path,
    monkeypatch,
) -> None:
    program = tmp_path / "penguin_burner.py"
    legacy_unit = tmp_path / "PenguinBurner.service"
    daemon_unit = tmp_path / "penguin-burnerd.service"
    state_file = tmp_path / "last-runtime.json"
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
    monkeypatch.setattr(runtime_service, "install_daemon_binary", lambda *_a, **_k: False)
    monkeypatch.setattr(
        runtime_service, "LAST_RUNTIME_STATE_PATH", state_file
    )
    monkeypatch.setattr(
        runtime_service,
        "clear_last_runtime_state",
        lambda: actions.append(("clear-last-runtime", [])),
    )
    monkeypatch.setattr(
        runtime_service,
        "_apply_persistent_runtime",
        lambda argv, **_kwargs: actions.append(("persist-runtime", list(argv)))
        or {"pid": 4321},
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

    assert (
        "run",
        ["/bin/systemctl", "disable", "--now", "PenguinBurner.service"],
    ) in actions
    assert (
        "checked",
        ["/bin/systemctl", "enable", "penguin-burnerd.service"],
    ) in actions
    assert (
        "checked",
        ["/bin/systemctl", "restart", "penguin-burnerd.service"],
    ) in actions
    assert actions.index(("clear-last-runtime", [])) < actions.index(
        (
            "checked",
            ["/bin/systemctl", "restart", "penguin-burnerd.service"],
        )
    )
    unit = daemon_unit.read_text(encoding="utf-8")
    assert (
        "ExecStart=/usr/libexec/penguin-burnerd --socket /run/penguin-burnerd.sock"
    ) in unit
    assert "PENGUIN_BURNER_DAEMON_AUTOSTART_ARGV_B64" not in unit
    assert ("persist-runtime", ["--auto-uv-profile", "profile-a"]) in actions
    assert actions.index(
        ("checked", ["/bin/systemctl", "restart", "penguin-burnerd.service"])
    ) < actions.index(("persist-runtime", ["--auto-uv-profile", "profile-a"]))
    assert any("persistent service install" in message for message in logs)


def test_install_systemd_service_restarts_active_daemon_after_unit_update(
    tmp_path: Path,
    monkeypatch,
) -> None:
    program = tmp_path / "penguin_burner.py"
    legacy_unit = tmp_path / "PenguinBurner.service"
    daemon_unit = tmp_path / "penguin-burnerd.service"
    state_file = tmp_path / "last-runtime.json"
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
    monkeypatch.setattr(runtime_service, "install_daemon_binary", lambda *_a, **_k: False)
    monkeypatch.setattr(
        runtime_service, "LAST_RUNTIME_STATE_PATH", state_file
    )
    monkeypatch.setattr(
        runtime_service,
        "clear_last_runtime_state",
        lambda: actions.append(("clear-last-runtime", [])),
    )
    monkeypatch.setattr(
        runtime_service,
        "_apply_persistent_runtime",
        lambda argv, **_kwargs: actions.append(("persist-runtime", list(argv)))
        or {"pid": 4321},
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
    assert actions.index(("clear-last-runtime", [])) < actions.index(
        ("checked", ["/bin/systemctl", "restart", "penguin-burnerd.service"])
    )
    assert waited == [runtime_service.DEFAULT_DAEMON_SOCKET]
    assert ("persist-runtime", ["--auto-uv-profile", "profile-a"]) in actions


def test_migrate_to_daemon_service_disables_legacy_after_daemon_reachable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    program = tmp_path / "penguin_burner.py"
    legacy_unit = tmp_path / "PenguinBurner.service"
    daemon_unit = tmp_path / "penguin-burnerd.service"
    state_file = tmp_path / "last-runtime.json"
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
    monkeypatch.setattr(runtime_service, "install_daemon_binary", lambda *_a, **_k: False)
    monkeypatch.setattr(
        runtime_service, "LAST_RUNTIME_STATE_PATH", state_file
    )
    monkeypatch.setattr(
        runtime_service,
        "clear_last_runtime_state",
        lambda: actions.append(("clear-last-runtime", [])),
    )
    monkeypatch.setattr(
        runtime_service,
        "_apply_persistent_runtime",
        lambda argv, **_kwargs: actions.append(("persist-runtime", list(argv)))
        or {"pid": 4321},
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
    unit_text = daemon_unit.read_text(encoding="utf-8")
    assert (
        "ExecStart=/usr/libexec/penguin-burnerd --socket /tmp/penguin-burnerd.sock"
    ) in unit_text
    assert "PENGUIN_BURNER_DAEMON_AUTOSTART_ARGV_B64" not in unit_text
    assert (
        "persist-runtime",
        ["--auto-uv-profile", "profile-a", "--adaptive-auto-uv"],
    ) in actions
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
        (
            "persist-runtime",
            ["--auto-uv-profile", "profile-a", "--adaptive-auto-uv"],
        )
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
    monkeypatch.setattr(runtime_service, "install_daemon_binary", lambda *_a, **_k: False)
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
        "_apply_runtime",
        lambda argv, **_kwargs: (
            starts.append(list(argv)) or {"pid": 4321},
            {"format_version": 1},
        ),
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
    assert "PENGUIN_BURNER_ADAPTIVE_TARGET_FPS" not in unit
    assert "Started runtime profile through penguin-burnerd.service" in "\n".join(logs)


@pytest.mark.parametrize(
    ("binary_changed", "expected_verb"),
    [(True, "restart"), (False, "start")],
)
def test_daemonize_restarts_only_when_existing_binary_changed(
    binary_changed: bool,
    expected_verb: str,
    tmp_path: Path,
    monkeypatch,
) -> None:
    program = tmp_path / "penguin_burner.py"
    daemon_unit = tmp_path / "penguin-burnerd.service"
    program.write_text("# program\n", encoding="utf-8")
    daemon_unit.write_text(
        runtime_service.build_daemon_api_service_unit(program),
        encoding="utf-8",
    )
    checked_calls = []
    monkeypatch.setattr(runtime_service, "SYSTEMCTL", "/bin/systemctl")
    monkeypatch.setattr(runtime_service, "systemd_is_available", lambda: True)
    monkeypatch.setattr(runtime_service.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        runtime_service,
        "install_daemon_binary",
        lambda *_args, **_kwargs: binary_changed,
    )
    monkeypatch.setattr(
        runtime_service,
        "clear_existing_penguin_burner_unit_for_daemonize",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        runtime_service, "daemon_systemd_service_unit_path", lambda: daemon_unit
    )
    monkeypatch.setattr(runtime_service, "_wait_for_daemon_status", lambda *_args: None)
    monkeypatch.setattr(
        runtime_service,
        "_apply_runtime",
        lambda *_args, **_kwargs: ({"pid": 4321}, {"format_version": 1}),
    )
    monkeypatch.setattr(
        runtime_service.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout="", stderr=""
        ),
    )
    monkeypatch.setattr(
        runtime_service,
        "run_checked_subprocess",
        lambda args: checked_calls.append(list(args))
        or SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    runtime_service.daemonize_with_systemd(
        program,
        ["--auto-uv-profile", "profile-a"],
        journal_hours=4,
        log=lambda _message: None,
    )

    expected = [
        "/bin/systemctl",
        expected_verb,
        "penguin-burnerd.service",
    ]
    assert expected in checked_calls
    assert ["/bin/systemctl", "enable", "penguin-burnerd.service"] not in checked_calls
    assert ["/bin/systemctl", "daemon-reload"] not in checked_calls
    other_verb = "start" if expected_verb == "restart" else "restart"
    assert [
        "/bin/systemctl",
        other_verb,
        "penguin-burnerd.service",
    ] not in checked_calls


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
    monkeypatch.setattr(runtime_service, "install_daemon_binary", lambda *_a, **_k: False)
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
        "_apply_runtime",
        lambda argv, **_kwargs: ({"pid": 4321}, {"format_version": 1}),
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


def test_flatpak_site_packages_stabilizes_deployment_id_to_active(monkeypatch) -> None:
    # The Python daemon unit is no longer built inside the Flatpak sandbox
    # (Option A -- the install path is gated in ui/commands.py), but the surviving
    # flatpak path helper still rewrites a concrete deployment-id path to the
    # stable "active" symlink so a re-sync does not strand PYTHONPATH.
    monkeypatch.setenv("FLATPAK_ID", "io.github.jpietek.PenguinBurner")
    monkeypatch.setenv("PENGUIN_BURNER_FLATPAK_APP_PATH", FLATPAK_DEPLOYMENT_APP_PATH)
    monkeypatch.delenv("PENGUIN_BURNER_FLATPAK_SITE_PACKAGES", raising=False)

    site_packages = str(
        runtime_service.flatpak_host_site_packages_path(
            "/app/lib/python3.13/site-packages/penguin_burner.py"
        )
    )

    assert FLATPAK_DEPLOYMENT_ID not in site_packages
    assert site_packages == FLATPAK_ACTIVE_SITE_PACKAGES


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


def test_read_legacy_last_runtime_argv_recovers_profile_not_stock(
    tmp_path: Path, monkeypatch
) -> None:
    import json

    state_file = tmp_path / "last-runtime.json"
    monkeypatch.setattr(runtime_service, "LAST_RUNTIME_STATE_PATH", state_file)

    # A 0.6.x apply-on-startup profile is recovered verbatim.
    state_file.write_text(
        json.dumps(
            {
                "argv": ["--auto-uv-profile", "profile-x", "--silent-fan-curve"],
                "program_file": "/x/penguin_burner.py",
            }
        ),
        encoding="utf-8",
    )
    assert runtime_service.read_legacy_last_runtime_argv() == [
        "--auto-uv-profile",
        "profile-x",
        "--silent-fan-curve",
    ]

    # A stock/reset action is NOT worth persisting as a boot profile.
    from profiles.uv.profile_store import STOCK_PROFILE_SELECTOR

    state_file.write_text(
        json.dumps({"argv": ["--auto-uv-profile", STOCK_PROFILE_SELECTOR]}),
        encoding="utf-8",
    )
    assert runtime_service.read_legacy_last_runtime_argv() == []

    # An argv with no profile selector, junk, or a missing file -> nothing.
    state_file.write_text(json.dumps({"argv": ["--gpu-index", "0"]}), encoding="utf-8")
    assert runtime_service.read_legacy_last_runtime_argv() == []
    state_file.unlink()
    assert runtime_service.read_legacy_last_runtime_argv() == []


def test_migrate_recovers_066_boot_profile_without_legacy_capitalized_unit(
    tmp_path: Path, monkeypatch
) -> None:
    import json

    program = tmp_path / "penguin_burner.py"
    program.write_text("# program\n", encoding="utf-8")
    daemon_unit = tmp_path / "penguin-burnerd.service"
    missing_legacy = tmp_path / "PenguinBurner.service"  # pre-0.6, absent
    state_file = tmp_path / "last-runtime.json"
    state_file.write_text(
        json.dumps(
            {"argv": ["--auto-uv-profile", "profile-066", "--adaptive-auto-uv"]}
        ),
        encoding="utf-8",
    )
    actions = []

    def fake_run(args, **_kwargs):
        actions.append(("run", list(args)))
        return SimpleNamespace(returncode=1, stdout="", stderr="")  # unit absent

    monkeypatch.setattr(runtime_service, "SYSTEMCTL", "/bin/systemctl")
    monkeypatch.setattr(runtime_service, "systemd_is_available", lambda: True)
    monkeypatch.setattr(runtime_service.os, "geteuid", lambda: 0)
    monkeypatch.setattr(runtime_service, "install_daemon_binary", lambda *_a, **_k: False)
    monkeypatch.setattr(runtime_service, "LAST_RUNTIME_STATE_PATH", state_file)
    monkeypatch.setattr(
        runtime_service, "clear_last_runtime_state", lambda: None
    )
    monkeypatch.setattr(
        runtime_service,
        "_apply_persistent_runtime",
        lambda argv, **_kwargs: actions.append(("persist-runtime", list(argv)))
        or {"pid": 1},
    )
    monkeypatch.setattr(
        runtime_service, "legacy_systemd_service_unit_path", lambda: missing_legacy
    )
    monkeypatch.setattr(
        runtime_service, "daemon_systemd_service_unit_path", lambda: daemon_unit
    )
    monkeypatch.setattr(runtime_service, "daemon_status", lambda **_kwargs: {"state": "idle"})
    monkeypatch.setattr(runtime_service.subprocess, "run", fake_run)
    monkeypatch.setattr(
        runtime_service, "run_checked_subprocess",
        lambda args: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    logs = []
    runtime_service.migrate_to_daemon_service(
        program, socket_path="/tmp/s.sock", log=logs.append
    )

    # The 0.6.x boot profile survives the upgrade instead of falling to stock.
    assert (
        "persist-runtime",
        ["--auto-uv-profile", "profile-066", "--adaptive-auto-uv"],
    ) in actions
    assert any("Recovered apply-on-startup" in line for line in logs)
