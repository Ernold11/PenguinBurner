from __future__ import annotations

from pathlib import Path
import shlex
import subprocess

from saved_uv_profiles import profile_display_name
from saved_uv_profiles import read_auto_uv_profile_summaries
from runtime_service import PENGUIN_BURNER_UNIT_NAME
from runtime_service import SYSTEMCTL
from runtime_service import systemd_service_unit_path

from .afterburner_import import afterburner_import_profile_summary


def load_profile_summaries() -> list[dict]:
    profiles = list(read_auto_uv_profile_summaries())
    afterburner_profile = afterburner_import_profile_summary()
    if afterburner_profile is not None:
        profiles.append(afterburner_profile)
    return profiles


def profile_for_selector(profiles: list[dict], selector: str) -> dict | None:
    text = str(selector or "").strip()
    if not text:
        return None
    if text in {"latest", "__systemd_default__"}:
        return profiles[0] if profiles else None
    for profile in profiles:
        path = str(profile.get("path", "")).strip()
        path_names = {Path(path).name, Path(path).stem} if path else set()
        if text in {
            str(profile.get("profile_id", "")),
            str(profile.get("candidate_id", "")),
            path,
            *path_names,
        }:
            return profile
    return None


def profiles_for_selectors(profiles: list[dict], selectors: list[str]) -> list[dict]:
    selected = []
    seen = set()
    for selector in selectors:
        profile = profile_for_selector(profiles, selector)
        if profile is None:
            continue
        profile_id = str(profile.get("profile_id", ""))
        if profile_id in seen:
            continue
        selected.append(profile)
        seen.add(profile_id)
    return selected


def selected_profile_ids_include_selector(
    profiles: list[dict],
    selected_ids: list[str],
    selector: str,
) -> bool:
    selected = {str(value) for value in selected_ids}
    if not selected or not str(selector or "").strip():
        return False
    profile = profile_for_selector(profiles, selector)
    return bool(profile and str(profile.get("profile_id", "")) in selected)


def profile_can_apply(profile: dict) -> bool:
    return profile_is_afterburner(profile) or bool(profile.get("final_verified", False))


def profile_can_verify(profile: dict) -> bool:
    return profile_is_afterburner(profile) or bool(str(profile.get("path", "")).strip())


def profile_verify_selector(profile: dict) -> str:
    if profile_is_afterburner(profile):
        return ""
    path = str(profile.get("path", "")).strip()
    if path:
        return path
    return str(profile.get("profile_id", "")).strip()


def profile_is_afterburner(profile: dict) -> bool:
    return str(profile.get("runtime_source", "")).strip() == "afterburner"


def profile_is_deletable(profile: dict) -> bool:
    return bool(str(profile.get("path", "")).strip()) or profile_is_afterburner(profile)


def profile_status_label(profiles: list[dict], selector: str) -> str:
    profile = profile_for_selector(profiles, selector)
    if profile is None:
        text = str(selector or "").strip()
        if text == "__systemd_default__":
            return "latest Auto-UV profile"
        return text or "unknown profile"
    display_name = str(profile.get("display_name", "")).strip()
    if display_name:
        return display_name
    text = profile_frequency_voltage(profile)
    return text or profile_display_name(profile) or str(profile.get("profile_id", ""))


def profile_frequency_voltage(profile: dict) -> str:
    clock = _status_number(profile.get("lock_clock_mhz"), precision=0)
    voltage = _status_number(profile.get("candidate_voltage_mv"), precision=0)
    if clock and voltage:
        return f"{clock} MHz {voltage} mV"
    return f"{clock} MHz" if clock else (f"{voltage} mV" if voltage else "")


def final_profile_notice_text(
    profiles: list[dict],
    *,
    profile_id: str = "",
    candidate_id: str = "",
    result_payload: dict | None = None,
) -> str:
    profile = profile_for_selector(profiles, profile_id) or profile_for_selector(
        profiles,
        candidate_id,
    )
    label = profile_frequency_voltage(profile) if profile is not None else ""
    label = label or final_result_frequency_voltage(result_payload or {})
    if label:
        return (
            f"Final verification complete. Profile {label} is saved and "
            "highlighted in Profiles."
        )
    return "Final verification complete. The saved profile is highlighted in Profiles."


def final_result_frequency_voltage(payload: dict) -> str:
    clock = _status_number(
        payload.get("clock_mhz", payload.get("lock_clock_mhz")),
        precision=0,
    )
    voltage = _status_number(
        payload.get("voltage_mv", payload.get("candidate_voltage_mv")),
        precision=0,
    )
    if clock and voltage:
        return f"{clock} MHz {voltage} mV"
    return f"{clock} MHz" if clock else (f"{voltage} mV" if voltage else "")


def runner_status_text(
    profiles: list[dict],
    *,
    running_selector: str = "",
    autostart_selector: str = "",
    running_silent_fan: bool = False,
    autostart_silent_fan: bool = False,
) -> str:
    running_selector = str(running_selector or "").strip()
    autostart_selector = str(autostart_selector or "").strip()
    if running_selector:
        autostarts = _profile_selectors_match(
            profiles,
            running_selector,
            autostart_selector,
        )
        parts = [
            f"Currently running profile: {profile_status_label(profiles, running_selector)}",
            f"Systemd autostart: {'Yes' if autostarts else 'No'}",
            f"Silent fan curve: {_on_off(running_silent_fan)}",
        ]
        if autostart_selector and not autostarts:
            parts.append(
                f"Autostart profile: {profile_status_label(profiles, autostart_selector)}"
            )
        return "; ".join(parts) + "."
    if autostart_selector:
        return (
            f"Autostart profile: {profile_status_label(profiles, autostart_selector)}; "
            "Systemd autostart: Yes; "
            f"Silent fan curve: {_on_off(autostart_silent_fan)}; "
            "Not running now."
        )
    return "No running/autostart profile available yet."


def systemd_autostart_profile_info() -> dict[str, object]:
    if not systemd_service_is_enabled():
        return {"selector": "", "silent_fan_curve": False}
    command = _systemd_unit_exec_start()
    return profile_info_from_command_text(command, default_if_present=True)


def running_auto_uv_profile_info() -> dict[str, object]:
    command = _systemd_running_exec_start()
    info = profile_info_from_command_text(command, default_if_present=True)
    if str(info["selector"]):
        return info
    return systemd_autostart_profile_info()


def profile_info_from_command_text(
    command_text: str,
    *,
    default_if_present: bool = False,
) -> dict[str, object]:
    parts = _command_parts(command_text)
    selector = _profile_selector_from_command_parts(parts)
    if not selector and default_if_present and str(command_text).strip():
        selector = "__systemd_default__"
    return {
        "selector": selector,
        "silent_fan_curve": "--silent-fan-curve" in parts,
    }


def systemd_unit_entry_exists() -> bool:
    try:
        return systemd_service_unit_path().is_file()
    except OSError:
        return False


def systemd_service_is_enabled() -> bool:
    return _systemctl_quiet("is-enabled")


def penguin_burner_runtime_is_active() -> bool:
    return _systemctl_quiet("is-active")


def delete_confirmation_text(
    names: list[str],
    *,
    removes_systemd: bool = False,
    includes_afterburner: bool = False,
) -> str:
    clean_names = [str(name).strip() for name in names if str(name).strip()]
    if not clean_names:
        subject = "the selected profiles"
    elif len(clean_names) == 1:
        label = "profile " if includes_afterburner else "Auto-UV profile "
        subject = f"{label}{clean_names[0]}"
    else:
        subject = f"{len(clean_names)} selected profiles"
    message = f"Delete {subject}?"
    if includes_afterburner:
        message += (
            "\n\nAuto-UV profile files are removed from disk. "
            "Afterburner import entries are removed from PenguinBurner's config."
        )
    if removes_systemd:
        message += (
            "\n\nThis profile is currently persisted on startup. Deleting it will "
            "remove the Systemd autostart entry too."
        )
    return message


def _profile_selectors_match(
    profiles: list[dict],
    left_selector: str,
    right_selector: str,
) -> bool:
    left = str(left_selector or "").strip()
    right = str(right_selector or "").strip()
    if not left or not right:
        return False
    if left == right:
        return True
    left_profile = profile_for_selector(profiles, left)
    right_profile = profile_for_selector(profiles, right)
    if left_profile is None or right_profile is None:
        return False
    return str(left_profile.get("profile_id", "")) == str(
        right_profile.get("profile_id", "")
    )


def _profile_selector_from_command_parts(parts: list[str]) -> str:
    if "--prefer-afterburner-curve" in parts:
        return "afterburner-import"
    for index, part in enumerate(parts):
        if part == "--auto-uv-profile" and index + 1 < len(parts):
            return str(parts[index + 1])
        if part.startswith("--auto-uv-profile="):
            return part.split("=", 1)[1]
    return ""


def _command_parts(command_text: str) -> list[str]:
    try:
        return shlex.split(str(command_text or ""))
    except ValueError:
        return str(command_text or "").split()


def _systemd_unit_exec_start() -> str:
    try:
        text = systemd_service_unit_path().read_text(
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return ""
    service_exists = bool(text.strip())
    for line in text.splitlines():
        if line.startswith("ExecStart="):
            return line.split("=", 1)[1]
    return "__systemd_default__" if service_exists else ""


def _systemd_running_exec_start() -> str:
    unit_name = f"{PENGUIN_BURNER_UNIT_NAME}.service"
    try:
        result = subprocess.run(
            [SYSTEMCTL, "show", unit_name, "--property=ExecStart", "--value"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=1.5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if int(result.returncode) != 0:
        return ""
    return result.stdout.strip()


def _systemctl_quiet(action: str) -> bool:
    unit_name = f"{PENGUIN_BURNER_UNIT_NAME}.service"
    try:
        result = subprocess.run(
            [SYSTEMCTL, str(action), "--quiet", unit_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=1.5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return int(result.returncode) == 0


def _status_number(value, *, precision: int) -> str:
    if value in (None, ""):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if precision <= 0:
        return str(int(round(number)))
    return f"{number:.{int(precision)}f}"


def _on_off(value: bool) -> str:
    return "On" if bool(value) else "Off"
