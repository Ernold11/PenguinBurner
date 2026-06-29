from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import pwd
import shutil
import sys
from typing import Mapping

from ui.constants import DEFAULT_FINAL_VERIFICATION_DURATION_S
from ui.features.tuning.gpu_selection import runtime_gpu_index


FLATPAK_INFO_PATH = Path("/.flatpak-info")
FLATPAK_APP_ID = "io.github.jpietek.PenguinBurner"


def running_in_flatpak() -> bool:
    return FLATPAK_INFO_PATH.is_file()


def cli_base_command() -> list[str]:
    script_path = Path(__file__).resolve().parents[1] / "penguin_burner.py"
    if script_path.is_file():
        return [sys.executable, str(script_path)]
    return ["penguin-burner-cli"]


def host_cli_base_command() -> list[str]:
    override = os.environ.get("PENGUIN_BURNER_HOST_CLI", "").strip()
    if override:
        return [override]
    if running_in_flatpak():
        flatpak = shutil.which("flatpak") or "/usr/bin/flatpak"
        return [
            flatpak,
            "run",
            "--user",
            "--command=penguin-burner-cli",
            os.environ.get("FLATPAK_ID", "").strip() or FLATPAK_APP_ID,
        ]
    return ["penguin-burner-cli"]


def _desktop_user_name() -> str:
    return (
        os.environ.get("PENGUIN_BURNER_Q2RTX_USER", "").strip()
        or os.environ.get("SUDO_USER", "").strip()
        or os.environ.get("USER", "").strip()
        or os.environ.get("LOGNAME", "").strip()
    )


def desktop_user_env() -> list[str]:
    user = _desktop_user_name()
    uid = (
        os.environ.get("PENGUIN_BURNER_Q2RTX_UID", "").strip()
        or os.environ.get("SUDO_UID", "").strip()
    )
    gid = (
        os.environ.get("PENGUIN_BURNER_Q2RTX_GID", "").strip()
        or os.environ.get("SUDO_GID", "").strip()
    )
    if not user and os.getuid() != 0:
        try:
            user = pwd.getpwuid(os.getuid()).pw_name
        except KeyError:
            user = ""
    if not uid and os.getuid() != 0:
        uid = str(os.getuid())
    if not gid and os.getgid() != 0:
        gid = str(os.getgid())

    values = []
    if user:
        values.append(f"PENGUIN_BURNER_Q2RTX_USER={user}")
        values.append(f"SUDO_USER={user}")
    if uid:
        values.append(f"PENGUIN_BURNER_Q2RTX_UID={uid}")
        values.append(f"SUDO_UID={uid}")
    if gid:
        values.append(f"PENGUIN_BURNER_Q2RTX_GID={gid}")
        values.append(f"SUDO_GID={gid}")
    return values


def desktop_session_env() -> list[str]:
    names = [
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XAUTHORITY",
        "XDG_CURRENT_DESKTOP",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
    ]
    values = []
    for name in names:
        if running_in_flatpak() and name == "DBUS_SESSION_BUS_ADDRESS":
            value = _host_session_bus_address()
        else:
            value = os.environ.get(name, "").strip()
        if value:
            values.append(f"{name}={value}")
    if os.environ.get("DISPLAY", "").strip() and not os.environ.get(
        "XAUTHORITY", ""
    ).strip():
        xauthority = _default_xauthority_path()
        if xauthority:
            values.append(f"XAUTHORITY={xauthority}")
    return values


def _default_xauthority_path() -> str:
    user = _desktop_user_name()
    home = ""
    if user:
        try:
            home = pwd.getpwnam(user).pw_dir
        except KeyError:
            home = ""
    if not home:
        return ""
    path = Path(home) / ".Xauthority"
    return str(path) if path.is_file() else ""


def _host_session_bus_address() -> str:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if runtime_dir.startswith("/run/user/"):
        return f"unix:path={runtime_dir}/bus"
    uid = (
        os.environ.get("PENGUIN_BURNER_Q2RTX_UID", "").strip()
        or os.environ.get("SUDO_UID", "").strip()
    )
    if not uid and os.getuid() != 0:
        uid = str(os.getuid())
    return f"unix:path=/run/user/{uid}/bus" if uid else ""


def _privileged_command_base() -> list[str] | None:
    if os.geteuid() == 0:
        return []
    if running_in_flatpak():
        flatpak_spawn = shutil.which("flatpak-spawn")
        if flatpak_spawn:
            return [flatpak_spawn, "--host", "/usr/bin/pkexec", "/usr/bin/env"]
        return None
    escalator = shutil.which("pkexec") or shutil.which("sudo")
    if not escalator:
        return None
    env = shutil.which("env") or "/usr/bin/env"
    return [escalator, env]


def _privileged_env() -> list[str]:
    values = []
    if running_in_flatpak():
        values.extend(_host_flatpak_user_env())
        values.append(_host_path_assignment())
        pythonpath = _host_pythonpath_assignment()
        if pythonpath:
            values.append(pythonpath)
    return [*values, *desktop_user_env(), *desktop_session_env()]


def _host_flatpak_user_env() -> list[str]:
    home = _desktop_user_home()
    if not home:
        return []
    return [
        f"HOME={home}",
        f"XDG_DATA_HOME={home}/.local/share",
    ]


def _desktop_user_home() -> str:
    override = os.environ.get("PENGUIN_BURNER_HOME", "").strip()
    if override:
        return str(Path(override).expanduser())
    user = _desktop_user_name()
    if user:
        try:
            return pwd.getpwnam(user).pw_dir
        except KeyError:
            pass
    uid = (
        os.environ.get("PENGUIN_BURNER_Q2RTX_UID", "").strip()
        or os.environ.get("SUDO_UID", "").strip()
    )
    if uid:
        try:
            return pwd.getpwuid(int(uid)).pw_dir
        except (KeyError, ValueError):
            pass
    home = str(Path.home())
    return home if home and home != "/" else ""


def _host_path_assignment() -> str:
    entries = []
    home = (_desktop_user_home() if running_in_flatpak() else str(Path.home())).strip()
    if home and home != "/":
        entries.append(str(Path(home) / ".local" / "bin"))
    entries.extend(["/usr/local/bin", "/usr/bin", "/bin"])
    for item in os.environ.get("PATH", "").split(os.pathsep):
        item = item.strip()
        if item and not item.startswith("/app") and item not in entries:
            entries.append(item)
    return "PATH=" + os.pathsep.join(entries)


def _host_pythonpath_assignment() -> str:
    entries = []
    home = Path.home()
    user_lib = home / ".local" / "lib"
    if user_lib.is_dir():
        entries.extend(
            str(path)
            for path in sorted(user_lib.glob("python*/site-packages"), reverse=True)
            if path.is_dir()
        )
    for item in os.environ.get("PYTHONPATH", "").split(os.pathsep):
        item = item.strip()
        if item and not item.startswith("/app") and item not in entries:
            entries.append(item)
    return "PYTHONPATH=" + os.pathsep.join(entries) if entries else ""


def _privileged_command(command: list[str]) -> list[str]:
    base = _privileged_command_base()
    if base is None:
        return list(command)
    if not base:
        return list(command)
    return [*base, *_privileged_env(), *command]


def scan_command(auto_uv_options: Mapping[str, object] | None = None) -> list[str]:
    options = auto_uv_options or {}
    payload = {
        "gpu_index": options.get("gpu_index", runtime_gpu_index()),
    }
    option_keys = (
        "auto_uv_mode",
        "auto_uv_min_voltage_mv",
        "auto_uv_max_clock_drop_pct",
        "auto_uv_memory_offset_mhz",
        "auto_uv_power_limit_w",
        "auto_uv_tail_rise_bins",
        "auto_oc_target_voltage_mv",
        "auto_oc_target_clock_mhz",
    )
    for key in option_keys:
        value = options.get(key)
        if value in (None, ""):
            continue
        payload[key] = value
    return [
        sys.executable,
        "-m",
        "runtime.daemon_client",
        "start-auto-uv",
        json.dumps(payload, separators=(",", ":")),
    ]


def privileged_command(command: list[str]) -> list[str]:
    if running_in_flatpak():
        command = _host_equivalent_command(command)
    return _privileged_command(command)


def _host_equivalent_command(command: list[str]) -> list[str]:
    command = list(command)
    local_base = cli_base_command()
    if command[: len(local_base)] == local_base:
        return [*host_cli_base_command(), *command[len(local_base) :]]
    return command


def daemon_migration_command() -> list[str]:
    if running_in_flatpak():
        return _flatpak_daemon_service_install_command()
    return privileged_command([*cli_base_command(), "--migrate-to-daemon-service"])


def _flatpak_daemon_service_install_command() -> list[str]:
    from runtime.support.runtime_service import build_daemon_api_service_unit

    unit = build_daemon_api_service_unit(sys.argv[0])
    encoded_unit = base64.b64encode(unit.encode("utf-8")).decode("ascii")
    script = r"""
unit=/etc/systemd/system/penguin-burnerd.service
tmp="$(mktemp /etc/systemd/system/.penguin-burnerd.service.XXXXXX)"
trap 'rm -f "$tmp"' EXIT
printf '%s' "$PENGUIN_BURNER_SYSTEMD_UNIT_B64" | base64 -d > "$tmp"
chmod 0644 "$tmp"
mv "$tmp" "$unit"
trap - EXIT
systemctl daemon-reload
systemctl reset-failed penguin-burnerd.service >/dev/null 2>&1 || true
systemctl enable --now penguin-burnerd.service
echo "Installed and started penguin-burnerd.service at $unit."
""".strip()
    return _privileged_command(
        [
            f"PENGUIN_BURNER_SYSTEMD_UNIT_B64={encoded_unit}",
            "/bin/sh",
            "-eu",
            "-c",
            script,
            "penguin-burner-daemon-install",
        ]
    )


def runtime_profile_command(
    action: str,
    *,
    profile_selector: str = "",
    silent_fan_curve: bool = False,
    adaptive_auto_uv: bool = False,
    gpu_index: int | None = None,
) -> list[str]:
    runtime_argv: list[str] = []
    if action == "daemonize":
        service_flag = "--daemonize"
    elif action == "install-systemd":
        service_flag = "--install-systemd-service"
    elif action == "uninstall-systemd":
        service_flag = "--uninstall-systemd-service"
    else:
        raise ValueError(f"unknown runtime profile action: {action}")
    if profile_selector and action != "uninstall-systemd":
        runtime_argv.extend(["--auto-uv-profile", str(profile_selector)])
    if silent_fan_curve and action != "uninstall-systemd":
        runtime_argv.append("--silent-fan-curve")
    if adaptive_auto_uv and action != "uninstall-systemd":
        runtime_argv.append("--adaptive-auto-uv")
    if gpu_index is not None:
        runtime_argv.extend(["--gpu-index", str(max(0, int(gpu_index)))])
    if running_in_flatpak():
        return _flatpak_systemd_profile_command(action, runtime_argv)
    command = [*cli_base_command(), service_flag, *runtime_argv]
    return privileged_command(command)


def _flatpak_systemd_profile_command(action: str, runtime_argv: list[str]) -> list[str]:
    if action == "install-systemd":
        return _flatpak_install_systemd_command(runtime_argv)
    if action == "uninstall-systemd":
        return _flatpak_uninstall_systemd_command()
    if action == "daemonize":
        return _flatpak_daemonize_command(runtime_argv)
    raise ValueError(f"unknown runtime profile action: {action}")


def _flatpak_install_systemd_command(runtime_argv: list[str]) -> list[str]:
    from runtime.support.runtime_service import build_systemd_service_unit

    unit = build_systemd_service_unit(sys.argv[0], list(runtime_argv))
    encoded_unit = base64.b64encode(unit.encode("utf-8")).decode("ascii")
    script = r"""
unit=/etc/systemd/system/PenguinBurner.service
systemctl disable --now PenguinBurner.service >/dev/null 2>&1 || true
if [ -f "$unit" ]; then
    rm -f "$unit"
    echo "Removed existing static PenguinBurner.service before persistent service install."
fi
tmp="$(mktemp /etc/systemd/system/.PenguinBurner.service.XXXXXX)"
trap 'rm -f "$tmp"' EXIT
printf '%s' "$PENGUIN_BURNER_SYSTEMD_UNIT_B64" | base64 -d > "$tmp"
chmod 0644 "$tmp"
mv "$tmp" "$unit"
trap - EXIT
systemctl daemon-reload
systemctl reset-failed PenguinBurner.service >/dev/null 2>&1 || true
systemctl enable --now PenguinBurner.service
echo "Installed and enabled PenguinBurner.service at $unit."
echo "Follow the journal with: journalctl -u PenguinBurner.service --since \"-4 hours\" -f"
""".strip()
    return _privileged_command(
        [
            f"PENGUIN_BURNER_SYSTEMD_UNIT_B64={encoded_unit}",
            "/bin/sh",
            "-eu",
            "-c",
            script,
            "penguin-burner-systemd-install",
        ]
    )


def _flatpak_uninstall_systemd_command() -> list[str]:
    script = r"""
unit=/etc/systemd/system/PenguinBurner.service
systemctl disable --now PenguinBurner.service >/dev/null 2>&1 || true
rm -f "$unit"
systemctl daemon-reload
systemctl reset-failed PenguinBurner.service >/dev/null 2>&1 || true
echo "Removed PenguinBurner.service."
""".strip()
    return _privileged_command(
        ["/bin/sh", "-eu", "-c", script, "penguin-burner-systemd-uninstall"]
    )


def _flatpak_daemonize_command(runtime_argv: list[str]) -> list[str]:
    script = r"""
unit=/etc/systemd/system/PenguinBurner.service
systemctl disable --now PenguinBurner.service >/dev/null 2>&1 || true
rm -f "$unit"
systemctl daemon-reload
systemctl reset-failed PenguinBurner.service >/dev/null 2>&1 || true
exec "$@"
""".strip()
    return _privileged_command(
        [
            "/bin/sh",
            "-eu",
            "-c",
            script,
            "penguin-burner-systemd-run",
            *_flatpak_systemd_run_command(runtime_argv),
        ]
    )


def _flatpak_systemd_run_command(runtime_argv: list[str]) -> list[str]:
    from runtime.support.runtime_service import (
        PENGUIN_BURNER_FOREGROUND_ENV,
        adaptive_policy_env_assignments,
        desktop_runtime_env_assignments,
    )

    command = [
        "/usr/bin/systemd-run",
        "--unit",
        "PenguinBurner",
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
    ]
    for assignment in [
        *desktop_runtime_env_assignments(),
        *adaptive_policy_env_assignments(),
    ]:
        command.extend(["--setenv", assignment])
    command.extend([*host_cli_base_command(), *runtime_argv])
    return command


def profile_verify_command(
    *,
    profile_selector: str = "",
    duration_s: int = DEFAULT_FINAL_VERIFICATION_DURATION_S,
    stop_request_path: str | Path = "",
    gpu_index: int | None = None,
) -> list[str]:
    duration_s = max(1, int(duration_s))
    command = [
        *cli_base_command(),
        "--stability-test",
        "--stability-seconds",
        str(duration_s),
        "--gpu-index",
        str(runtime_gpu_index() if gpu_index is None else max(0, int(gpu_index))),
    ]
    if profile_selector:
        command.extend(["--auto-uv-profile", str(profile_selector)])
    if str(stop_request_path).strip():
        command.extend(["--stability-stop-request-file", str(stop_request_path)])
    return privileged_command(command)


def delete_profiles_command(profile_paths: list[str]) -> list[str]:
    command = [
        *cli_base_command(),
        "--delete-auto-uv-profiles",
        *[str(path) for path in profile_paths],
    ]
    return privileged_command(command)
