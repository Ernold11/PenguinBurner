from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path
import pwd


OVERLAY_STATE_ENV = "PENGUIN_BURNER_OVERLAY_STATE"
OVERLAY_TEXT_ENV = "PENGUIN_BURNER_OVERLAY_TEXT"
OVERLAY_ENABLE_ENV = "PENGUIN_BURNER_OVERLAY"
OVERLAY_ENABLE_ENV_ALIAS = "PB_OVERLAY"


def _home_overlay_dir(env: Mapping[str, str]) -> Path | None:
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


def overlay_state_path(env: Mapping[str, str] | None = None) -> Path:
    resolved_env = os.environ if env is None else env
    explicit = str(resolved_env.get(OVERLAY_STATE_ENV) or "").strip()
    if explicit:
        return Path(explicit).expanduser()

    # The game runs inside Steam's pressure-vessel container, where
    # XDG_RUNTIME_DIR holds only the sockets the container forwards;
    # the home directory is shared, so prefer it for the state files.
    home_dir = _home_overlay_dir(resolved_env)
    if home_dir is not None:
        return home_dir / "overlay-state.txt"

    runtime_dir = str(resolved_env.get("XDG_RUNTIME_DIR") or "").strip()
    if runtime_dir:
        return Path(runtime_dir) / "penguin-burner" / "overlay-state.txt"

    if os.getuid() == 0:
        sudo_uid = str(resolved_env.get("SUDO_UID") or "").strip()
        if sudo_uid.isdigit():
            candidate = Path("/run/user") / sudo_uid
            if candidate.exists():
                return candidate / "penguin-burner" / "overlay-state.txt"
        sudo_user = str(resolved_env.get("SUDO_USER") or "").strip()
        if sudo_user:
            try:
                candidate = Path("/run/user") / str(pwd.getpwnam(sudo_user).pw_uid)
            except KeyError:
                candidate = Path()
            if candidate.exists():
                return candidate / "penguin-burner" / "overlay-state.txt"

    return Path(f"/tmp/penguin-burner-overlay-{os.getuid()}.txt")


def overlay_text_path(env: Mapping[str, str] | None = None) -> Path:
    resolved_env = os.environ if env is None else env
    explicit = str(resolved_env.get(OVERLAY_TEXT_ENV) or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    state_path = overlay_state_path(resolved_env)
    return state_path.with_name("overlay-text.txt")


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


def _unescape_value(value: str) -> str:
    return str(value).replace("\\n", "\n").replace("\\\\", "\\")
