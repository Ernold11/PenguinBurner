from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import pwd
import time

from penguin_burner_paths import claim_desktop_user_ownership


OVERLAY_STATE_ENV = "PENGUIN_BURNER_OVERLAY_STATE"
OVERLAY_TEXT_ENV = "PENGUIN_BURNER_OVERLAY_TEXT"
OVERLAY_ENABLE_ENV = "PENGUIN_BURNER_OVERLAY"
OVERLAY_ENABLE_ENV_ALIAS = "PB_OVERLAY"


@dataclass(frozen=True, slots=True)
class OverlayState:
    gpu_index: int
    clock_mhz: int | None
    voltage_mv: int | None
    profile_tier: str
    power_w: int | None = None
    gpu_util_pct: int | None = None
    cpu_util_pct: int | None = None
    cpu_peak_thread_pct: int | None = None
    fan_pct: int | None = None
    temperature_c: int | None = None
    uv_offset_mv: int | None = None
    present_fps: str = ""
    framegen_fps: str = ""
    framegen_active: bool = False
    latency_ms: str = ""
    display_latency_ms: str = ""
    profile_tier_key: str = ""
    profile_id: str = ""
    adaptive: bool = False
    updated_unix_ns: int = 0


def _home_overlay_dir(env: dict[str, str]) -> Path | None:
    home = str(env.get("HOME") or "").strip()
    if home and home != "/root":
        return Path(home).expanduser() / ".cache" / "penguin-burner"

    sudo_uid = str(env.get("SUDO_UID") or "").strip()
    if sudo_uid.isdigit():
        try:
            user_home = pwd.getpwuid(int(sudo_uid)).pw_dir
        except KeyError:
            user_home = ""
        if user_home:
            return Path(user_home) / ".cache" / "penguin-burner"

    sudo_user = str(env.get("SUDO_USER") or "").strip()
    if sudo_user:
        try:
            user_home = pwd.getpwnam(sudo_user).pw_dir
        except KeyError:
            user_home = ""
        if user_home:
            return Path(user_home) / ".cache" / "penguin-burner"

    candidates: list[Path] = []
    try:
        candidates.append(Path.cwd())
    except OSError:
        pass
    try:
        candidates.append(Path(__file__).resolve())
    except OSError:
        pass
    for base in candidates:
        for candidate in (base, *base.parents):
            if candidate.parent == Path("/home"):
                return candidate / ".cache" / "penguin-burner"
    return None


def overlay_state_path(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    explicit = str(env.get(OVERLAY_STATE_ENV) or "").strip()
    if explicit:
        return Path(explicit).expanduser()

    # The game runs inside Steam's pressure-vessel container, where
    # XDG_RUNTIME_DIR holds only the sockets the container forwards;
    # the home directory is shared, so prefer it for the state files.
    home_dir = _home_overlay_dir(env)
    if home_dir is not None:
        return home_dir / "overlay-state.txt"

    runtime_dir = str(env.get("XDG_RUNTIME_DIR") or "").strip()
    if runtime_dir:
        return Path(runtime_dir) / "penguin-burner" / "overlay-state.txt"

    if os.getuid() == 0:
        sudo_uid = str(env.get("SUDO_UID") or "").strip()
        if sudo_uid.isdigit():
            candidate = Path("/run/user") / sudo_uid
            if candidate.exists():
                return candidate / "penguin-burner" / "overlay-state.txt"
        sudo_user = str(env.get("SUDO_USER") or "").strip()
        if sudo_user:
            try:
                candidate = Path("/run/user") / str(pwd.getpwnam(sudo_user).pw_uid)
            except KeyError:
                candidate = Path()
            if candidate.exists():
                return candidate / "penguin-burner" / "overlay-state.txt"

    return Path(f"/tmp/penguin-burner-overlay-{os.getuid()}.txt")


def overlay_text_path(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    explicit = str(env.get(OVERLAY_TEXT_ENV) or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    state_path = overlay_state_path(env)
    return state_path.with_name("overlay-text.txt")


def write_overlay_state(
    state: OverlayState,
    *,
    path: str | Path | None = None,
) -> Path:
    state_path = Path(path).expanduser() if path is not None else overlay_state_path()
    updated_unix_ns = (
        int(state.updated_unix_ns)
        if int(state.updated_unix_ns or 0) > 0
        else time.time_ns()
    )
    payload = {
        "version": "1",
        "updated_unix_ns": str(updated_unix_ns),
        "gpu_index": str(int(state.gpu_index)),
        "clock_mhz": _value_text(state.clock_mhz),
        "voltage_mv": _value_text(state.voltage_mv),
        "power_w": _value_text(state.power_w),
        "gpu_util_pct": _value_text(state.gpu_util_pct),
        "cpu_util_pct": _value_text(state.cpu_util_pct),
        "cpu_peak_thread_pct": _value_text(state.cpu_peak_thread_pct),
        "fan_pct": _value_text(state.fan_pct),
        "temperature_c": _value_text(state.temperature_c),
        "uv_offset_mv": _value_text(state.uv_offset_mv),
        "present_fps": str(state.present_fps or ""),
        "framegen_fps": str(state.framegen_fps or ""),
        "framegen_active": "1" if bool(state.framegen_active) else "0",
        "latency_ms": str(state.latency_ms or ""),
        "display_latency_ms": str(state.display_latency_ms or ""),
        "profile_tier": str(state.profile_tier or ""),
        "profile_tier_key": str(state.profile_tier_key or ""),
        "profile_id": str(state.profile_id or ""),
        "adaptive": "1" if bool(state.adaptive) else "0",
    }
    text = "".join(f"{key}={_escape_value(value)}\n" for key, value in payload.items())
    state_path.parent.mkdir(parents=True, exist_ok=True)
    claim_desktop_user_ownership(state_path.parent, include_parents=True)
    temp_path = state_path.with_name(state_path.name + ".tmp")
    temp_path.write_text(text, encoding="utf-8")
    temp_path.replace(state_path)
    claim_desktop_user_ownership(state_path)
    return state_path


def read_overlay_state(path: str | Path | None = None) -> dict[str, str]:
    state_path = Path(path).expanduser() if path is not None else overlay_state_path()
    try:
        lines = state_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in lines:
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        values[key] = _unescape_value(value.strip())
    return values


def _value_text(value: object | None) -> str:
    if value in (None, ""):
        return ""
    try:
        return str(int(round(float(value))))
    except (TypeError, ValueError):
        return ""


def _escape_value(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n")


def _unescape_value(value: str) -> str:
    return str(value).replace("\\n", "\n").replace("\\\\", "\\")
