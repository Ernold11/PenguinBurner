from __future__ import annotations

import os
from pathlib import Path
import pwd


def latency_socket_path(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    explicit = str(env.get("PENGUIN_BURNER_LATENCY_SOCKET") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    runtime_dir = str(env.get("XDG_RUNTIME_DIR") or "").strip()
    if runtime_dir:
        return Path(runtime_dir) / "penguin-burner" / "latency.sock"
    if os.getuid() == 0:
        sudo_uid = str(env.get("SUDO_UID") or "").strip()
        if sudo_uid.isdigit():
            candidate = Path("/run/user") / sudo_uid
            if candidate.exists():
                return candidate / "penguin-burner" / "latency.sock"
        sudo_user = str(env.get("SUDO_USER") or "").strip()
        if sudo_user:
            try:
                candidate = Path("/run/user") / str(pwd.getpwnam(sudo_user).pw_uid)
            except KeyError:
                candidate = Path()
            if candidate.exists():
                return candidate / "penguin-burner" / "latency.sock"
    return Path(f"/tmp/penguin-burner-latency-{os.getuid()}.sock")


def _home_latency_socket_path(env: dict[str, str]) -> Path | None:
    home = str(env.get("HOME") or "").strip()
    if home and home != "/root":
        return Path(home).expanduser() / ".cache" / "penguin-burner" / "latency.sock"

    sudo_uid = str(env.get("SUDO_UID") or "").strip()
    if sudo_uid.isdigit():
        try:
            user_home = pwd.getpwuid(int(sudo_uid)).pw_dir
        except KeyError:
            user_home = ""
        if user_home:
            return Path(user_home) / ".cache" / "penguin-burner" / "latency.sock"

    sudo_user = str(env.get("SUDO_USER") or "").strip()
    if sudo_user:
        try:
            user_home = pwd.getpwnam(sudo_user).pw_dir
        except KeyError:
            user_home = ""
        if user_home:
            return Path(user_home) / ".cache" / "penguin-burner" / "latency.sock"

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
                return candidate / ".cache" / "penguin-burner" / "latency.sock"
    return None


def latency_socket_paths(env: dict[str, str] | None = None) -> list[Path]:
    env = os.environ if env is None else env
    paths = [latency_socket_path(env)]
    if not str(env.get("PENGUIN_BURNER_LATENCY_SOCKET") or "").strip():
        home_path = _home_latency_socket_path(env)
        if home_path is not None:
            paths.append(home_path)

    unique_paths: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key not in seen:
            unique_paths.append(path)
            seen.add(key)
    return unique_paths
