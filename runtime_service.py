#!/usr/bin/env python3

import os
from pathlib import Path
import pwd
import shlex
import shutil
import subprocess


SYSTEMD_RUN = shutil.which("systemd-run") or "systemd-run"
SYSTEMCTL = shutil.which("systemctl") or "systemctl"
BASH = shutil.which("bash") or "/usr/bin/bash"
PENGUIN_BURNER_UNIT_NAME = "PenguinBurner"
PENGUIN_BURNER_FOREGROUND_ENV = "PENGUIN_BURNER_FOREGROUND"
DEFAULT_JOURNAL_HOURS = 4


def parse_runtime_flags(argv, *, default_journal_hours=DEFAULT_JOURNAL_HOURS):
    foreground = False
    daemonize = False
    install_systemd_service = False
    uninstall_systemd_service = False
    journal_hours = default_journal_hours
    passthrough = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--foreground":
            foreground = True
            index += 1
            continue
        if arg == "--daemonize":
            daemonize = True
            index += 1
            continue
        if arg == "--install-systemd-service":
            install_systemd_service = True
            index += 1
            continue
        if arg == "--uninstall-systemd-service":
            uninstall_systemd_service = True
            index += 1
            continue
        if arg == "--journal-hours":
            if index + 1 >= len(argv):
                raise RuntimeError("--journal-hours requires a value")
            index += 1
            arg = f"--journal-hours={argv[index]}"
        if arg.startswith("--journal-hours="):
            value = arg.split("=", 1)[1].strip()
            if not value:
                raise RuntimeError("--journal-hours requires a value")
            journal_hours = max(1, int(float(value)))
            index += 1
            continue
        passthrough.append(arg)
        index += 1
    if install_systemd_service and uninstall_systemd_service:
        raise RuntimeError(
            "choose either --install-systemd-service or --uninstall-systemd-service"
        )
    return {
        "foreground": foreground,
        "daemonize": daemonize,
        "install_systemd_service": install_systemd_service,
        "uninstall_systemd_service": uninstall_systemd_service,
        "journal_hours": journal_hours,
        "passthrough": passthrough,
    }


def running_under_systemd_service():
    return os.environ.get(PENGUIN_BURNER_FOREGROUND_ENV) == "1"


def systemd_is_available():
    return (
        Path("/run/systemd/system").exists() and shutil.which("systemd-run") is not None
    )


def journalctl_follow_command(hours):
    return f'journalctl -u {PENGUIN_BURNER_UNIT_NAME}.service --since "-{int(hours)} hours" -f'


def systemd_service_unit_path():
    return Path("/etc/systemd/system") / f"{PENGUIN_BURNER_UNIT_NAME}.service"


def _invoking_user_name():
    sudo_user = os.environ.get("SUDO_USER", "").strip()
    if sudo_user:
        return sudo_user
    return pwd.getpwuid(os.getuid()).pw_name


def launcher_script_path(program_file):
    path = Path(program_file).resolve().with_name("penguin_burner.sh")
    if not path.is_file():
        raise RuntimeError(f"launcher script not found: {path}")
    return path


def _format_systemd_exec(args):
    rendered = []
    for arg in args:
        text = str(arg).replace("%", "%%")
        rendered.append(shlex.quote(text))
    return " ".join(rendered)


def run_checked_subprocess(args):
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        output = (result.stdout.strip() or result.stderr.strip()).strip()
        command_text = " ".join(shlex.quote(str(arg)) for arg in args)
        raise RuntimeError(f"{command_text} failed: {output or result.returncode}")
    return result


def build_systemd_service_unit(program_file, argv):
    script_path = launcher_script_path(program_file)
    sudo_user = _invoking_user_name()
    exec_start = _format_systemd_exec([BASH, str(script_path), "--foreground", *argv])
    return (
        "[Unit]\n"
        "Description=PenguinBurner runtime daemon\n"
        "After=multi-user.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"Environment={PENGUIN_BURNER_FOREGROUND_ENV}=1\n"
        f"Environment=SUDO_USER={sudo_user}\n"
        "WorkingDirectory=/\n"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n"
        "RestartSec=2\n"
        "StandardOutput=journal\n"
        "StandardError=journal\n"
        f"SyslogIdentifier={PENGUIN_BURNER_UNIT_NAME}\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def install_systemd_service(program_file, argv, *, journal_hours, log):
    if not systemd_is_available():
        raise RuntimeError("systemd service install is unavailable on this system.")
    if os.geteuid() != 0:
        raise RuntimeError(
            "systemd service install requires root privileges. Re-run with sudo."
        )

    unit_path = systemd_service_unit_path()
    unit_path.write_text(build_systemd_service_unit(program_file, argv))
    run_checked_subprocess([SYSTEMCTL, "daemon-reload"])
    subprocess.run(
        [SYSTEMCTL, "reset-failed", unit_path.name],
        capture_output=True,
        text=True,
        check=False,
    )
    run_checked_subprocess([SYSTEMCTL, "enable", "--now", unit_path.name])
    log(f"Installed and enabled {unit_path.name} at {unit_path}.")
    log(f"Follow the journal with: {journalctl_follow_command(journal_hours)}")


def uninstall_systemd_service(*, log):
    if not systemd_is_available():
        raise RuntimeError("systemd service uninstall is unavailable on this system.")
    if os.geteuid() != 0:
        raise RuntimeError(
            "systemd service uninstall requires root privileges. Re-run with sudo."
        )

    unit_path = systemd_service_unit_path()
    subprocess.run([SYSTEMCTL, "disable", "--now", unit_path.name], check=False)
    if unit_path.exists():
        unit_path.unlink()
    run_checked_subprocess([SYSTEMCTL, "daemon-reload"])
    subprocess.run(
        [SYSTEMCTL, "reset-failed", unit_path.name],
        capture_output=True,
        text=True,
        check=False,
    )
    log(f"Removed {unit_path.name}.")


def clear_existing_penguin_burner_unit_for_daemonize(*, log):
    unit_name = f"{PENGUIN_BURNER_UNIT_NAME}.service"
    unit_path = systemd_service_unit_path()

    subprocess.run(
        [SYSTEMCTL, "disable", "--now", unit_name],
        capture_output=True,
        text=True,
        check=False,
    )
    if unit_path.exists():
        unit_path.unlink()
        log(f"Removed existing static {unit_name} before transient daemon start.")
    run_checked_subprocess([SYSTEMCTL, "daemon-reload"])
    subprocess.run(
        [SYSTEMCTL, "reset-failed", unit_name],
        capture_output=True,
        text=True,
        check=False,
    )


def daemonize_with_systemd(program_file, argv, *, journal_hours, log):
    if not systemd_is_available():
        raise RuntimeError(
            "systemd background mode is unavailable on this system. "
            "Run PenguinBurner with --foreground or use a systemd-based system."
        )
    if os.geteuid() != 0:
        raise RuntimeError(
            "automatic systemd daemon mode requires root privileges. "
            "Re-run PenguinBurner with sudo."
        )

    script_path = launcher_script_path(program_file)
    sudo_user = os.environ.get("SUDO_USER", "").strip()

    clear_existing_penguin_burner_unit_for_daemonize(log=log)

    result = subprocess.run(
        [
            SYSTEMD_RUN,
            "--unit",
            PENGUIN_BURNER_UNIT_NAME,
            "--collect",
            "--service-type=simple",
            "--description",
            "PenguinBurner runtime daemon",
            "--property=WorkingDirectory=/",
            "--property=Restart=on-failure",
            "--property=RestartSec=2",
            "--property=StandardOutput=journal",
            "--property=StandardError=journal",
            "--property=SyslogIdentifier=PenguinBurner",
            "--setenv",
            f"{PENGUIN_BURNER_FOREGROUND_ENV}=1",
            "--setenv",
            f"SUDO_USER={sudo_user}",
            BASH,
            str(script_path),
            "--foreground",
            *argv,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    output = (result.stdout.strip() or result.stderr.strip()).strip()
    if result.returncode != 0:
        raise RuntimeError(
            "failed to daemonize PenguinBurner with systemd: "
            + (output or str(result.returncode))
        )

    unit_name = f"{PENGUIN_BURNER_UNIT_NAME}.service"
    if output:
        log(output)
    log(f"PenguinBurner daemonized under systemd as {unit_name}.")
    log(f"Follow the journal with: {journalctl_follow_command(journal_hours)}")
