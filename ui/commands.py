from __future__ import annotations

import os
from pathlib import Path
import pwd
import shutil
import sys
from typing import Mapping

from .constants import DEFAULT_FINAL_VERIFICATION_DURATION_S
from .gpu_selection import runtime_gpu_index


def cli_base_command() -> list[str]:
    script_path = Path(__file__).resolve().parents[1] / "penguin_burner.py"
    if script_path.is_file():
        return [sys.executable, str(script_path)]
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


def _command_value_text(value: object) -> str:
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def scan_command(auto_uv_options: Mapping[str, object] | None = None) -> list[str]:
    options = auto_uv_options or {}
    command = [
        *cli_base_command(),
        "--auto-uv-voltage-scan",
        "--json-events",
        "--auto-uv-require-final-choice",
        "--gpu-index",
        _command_value_text(options.get("gpu_index", runtime_gpu_index())),
    ]
    option_flags = {
        "auto_uv_mode": "--auto-uv-mode",
        "auto_uv_min_voltage_mv": "--auto-uv-min-voltage-mv",
        "auto_uv_max_drop_pct": "--auto-uv-max-drop-pct",
        "auto_uv_max_clock_drop_pct": "--auto-uv-max-clock-drop-pct",
        "auto_uv_short_seconds": "--auto-uv-short-seconds",
        "auto_uv_memory_offset_mhz": "--auto-uv-memory-offset-mhz",
        "auto_uv_tail_rise_bins": "--auto-uv-tail-rise-bins",
        "auto_oc_target_voltage_mv": "--auto-oc-target-voltage-mv",
        "auto_oc_target_clock_mhz": "--auto-oc-target-clock-mhz",
    }
    for key, flag in option_flags.items():
        value = options.get(key)
        if value in (None, ""):
            continue
        command.extend([flag, _command_value_text(value)])
    if os.geteuid() == 0:
        return command

    escalator = shutil.which("pkexec") or shutil.which("sudo")
    if escalator:
        env = shutil.which("env") or "/usr/bin/env"
        return [escalator, env, *desktop_user_env(), *desktop_session_env(), *command]
    return command


def privileged_command(command: list[str]) -> list[str]:
    if os.geteuid() == 0:
        return list(command)
    escalator = shutil.which("pkexec") or shutil.which("sudo")
    if not escalator:
        return list(command)
    env = shutil.which("env") or "/usr/bin/env"
    return [escalator, env, *desktop_user_env(), *desktop_session_env(), *command]


def runtime_profile_command(
    action: str,
    *,
    profile_selector: str = "",
    silent_fan_curve: bool = False,
    adaptive_auto_uv: bool = False,
    gpu_index: int | None = None,
) -> list[str]:
    command = [*cli_base_command()]
    if action == "daemonize":
        command.append("--daemonize")
    elif action == "install-systemd":
        command.append("--install-systemd-service")
    elif action == "uninstall-systemd":
        command.append("--uninstall-systemd-service")
    else:
        raise ValueError(f"unknown runtime profile action: {action}")
    if profile_selector and action != "uninstall-systemd":
        command.extend(["--auto-uv-profile", str(profile_selector)])
    if silent_fan_curve and action != "uninstall-systemd":
        command.append("--silent-fan-curve")
    if adaptive_auto_uv and action != "uninstall-systemd":
        command.append("--adaptive-auto-uv")
    if gpu_index is not None:
        command.extend(["--gpu-index", str(max(0, int(gpu_index)))])
    return privileged_command(command)


def profile_verify_command(
    *,
    profile_selector: str = "",
    duration_s: int = DEFAULT_FINAL_VERIFICATION_DURATION_S,
    stop_request_path: str | Path = "",
    q2rtx_enabled: bool = True,
    cuda_enabled: bool = True,
    gpu_index: int | None = None,
) -> list[str]:
    duration_s = max(1, int(duration_s))
    workload = _stability_workload_value(
        q2rtx_enabled=q2rtx_enabled,
        cuda_enabled=cuda_enabled,
    )
    command = [
        *cli_base_command(),
        "--stability-test",
        "--stability-seconds",
        str(duration_s),
        "--gpu-index",
        str(runtime_gpu_index() if gpu_index is None else max(0, int(gpu_index))),
    ]
    if workload != "q2rtx-cuda":
        command.extend(["--stability-workload", workload])
    if profile_selector:
        command.extend(["--auto-uv-profile", str(profile_selector)])
    if str(stop_request_path).strip():
        command.extend(["--stability-stop-request-file", str(stop_request_path)])
    return privileged_command(command)


def _stability_workload_value(
    *,
    q2rtx_enabled: bool = True,
    cuda_enabled: bool = True,
) -> str:
    if q2rtx_enabled and cuda_enabled:
        return "q2rtx-cuda"
    if q2rtx_enabled:
        return "q2rtx"
    if cuda_enabled:
        return "cuda"
    raise ValueError("at least one stability workload must be enabled")


def delete_profiles_command(profile_paths: list[str]) -> list[str]:
    command = [
        *cli_base_command(),
        "--delete-auto-uv-profiles",
        *[str(path) for path in profile_paths],
    ]
    return privileged_command(command)
