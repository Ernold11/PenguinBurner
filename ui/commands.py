from __future__ import annotations

import os
from pathlib import Path
import pwd
import shutil
import sys
from typing import Mapping


def cli_base_command() -> list[str]:
    script_path = Path(__file__).resolve().parents[1] / "penguin_burner.py"
    if script_path.is_file():
        return [sys.executable, str(script_path)]
    return ["penguin-burner-cli"]


def desktop_user_env() -> list[str]:
    user = (
        os.environ.get("PENGUIN_BURNER_Q2RTX_USER", "").strip()
        or os.environ.get("SUDO_USER", "").strip()
        or os.environ.get("USER", "").strip()
        or os.environ.get("LOGNAME", "").strip()
    )
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
    return values


def _command_value_text(value: object) -> str:
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def scan_command(auto_uv_options: Mapping[str, object] | None = None) -> list[str]:
    command = [
        *cli_base_command(),
        "--auto-uv-voltage-scan",
        "--json-events",
        "--auto-uv-require-final-choice",
    ]
    if bool((auto_uv_options or {}).get("auto_uv3")) or os.environ.get(
        "PENGUIN_BURNER_AUTO_UV3",
        "",
    ).strip() in {"1", "true", "yes", "on"}:
        command.append("--auto-uv3")
    option_flags = {
        "auto_uv_mode": "--auto-uv-mode",
        "auto_uv_max_drop_pct": "--auto-uv-max-drop-pct",
        "auto_uv_max_clock_drop_pct": "--auto-uv-max-clock-drop-pct",
        "auto_uv_clock_bump_budget_ratio": "--auto-uv-overclock-budget-ratio",
        "auto_uv_short_seconds": "--auto-uv-short-seconds",
        "auto_uv_memory_offset_mhz": "--auto-uv-memory-offset-mhz",
        "auto_uv_tail_rise_bins": "--auto-uv-tail-rise-bins",
    }
    boolean_flags = {
        "auto_uv_yolo": "--yolo",
    }
    for key, flag in boolean_flags.items():
        if bool((auto_uv_options or {}).get(key)):
            command.append(flag)
    for key, flag in option_flags.items():
        value = (auto_uv_options or {}).get(key)
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
    prefer_afterburner_curve: bool = False,
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
    if prefer_afterburner_curve and action != "uninstall-systemd":
        command.append("--prefer-afterburner-curve")
    if silent_fan_curve and action != "uninstall-systemd":
        command.append("--silent-fan-curve")
    return privileged_command(command)


def profile_verify_command(
    *,
    profile_selector: str = "",
    duration_s: int = 600,
    prefer_afterburner_curve: bool = False,
    stop_request_path: str | Path = "",
    q2rtx_enabled: bool = True,
    cuda_enabled: bool = True,
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
    ]
    if workload != "q2rtx-cuda":
        command.extend(["--stability-workload", workload])
    if profile_selector and not prefer_afterburner_curve:
        command.extend(["--auto-uv-profile", str(profile_selector)])
    if prefer_afterburner_curve:
        command.append("--prefer-afterburner-curve")
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
