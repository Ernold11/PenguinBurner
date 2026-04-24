from __future__ import annotations

import os
from pathlib import Path
import pwd
from typing import Callable

from .models import StabilityTestError


def _resolve_q2rtx_run_identity() -> dict | None:
    if os.geteuid() != 0:
        return None

    env_user = (
        os.environ.get("PENGUIN_BURNER_Q2RTX_USER", "").strip()
        or os.environ.get("SUDO_USER", "").strip()
    )
    env_uid = (
        os.environ.get("PENGUIN_BURNER_Q2RTX_UID", "").strip()
        or os.environ.get("SUDO_UID", "").strip()
    )
    env_gid = (
        os.environ.get("PENGUIN_BURNER_Q2RTX_GID", "").strip()
        or os.environ.get("SUDO_GID", "").strip()
    )

    pw_entry = None
    if env_user:
        try:
            pw_entry = pwd.getpwnam(env_user)
        except KeyError as exc:
            raise StabilityTestError(
                f"Q2RTX drop-user target was not found: {env_user}"
            ) from exc
    elif env_uid:
        try:
            pw_entry = pwd.getpwuid(int(env_uid))
        except (KeyError, ValueError) as exc:
            raise StabilityTestError(
                f"Q2RTX drop-user uid was not found: {env_uid}"
            ) from exc

    if pw_entry is None:
        raise StabilityTestError(
            "Q2RTX must run as a non-root desktop user, but no sudo/invoking user "
            "was found. Re-run with sudo so SUDO_USER is available, or set "
            "PENGUIN_BURNER_Q2RTX_USER."
        )

    uid = int(env_uid) if env_uid else int(pw_entry.pw_uid)
    gid = int(env_gid) if env_gid else int(pw_entry.pw_gid)
    if uid == 0:
        raise StabilityTestError("Q2RTX target user must not be root")

    home = Path(pw_entry.pw_dir).expanduser()
    runtime_dir = Path("/run/user") / str(uid)
    child_env = {
        "HOME": str(home),
        "USER": pw_entry.pw_name,
        "LOGNAME": pw_entry.pw_name,
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_CACHE_HOME": str(home / ".cache"),
    }
    if runtime_dir.is_dir():
        child_env["XDG_RUNTIME_DIR"] = str(runtime_dir)
    xauthority = home / ".Xauthority"
    if "XAUTHORITY" not in os.environ and xauthority.is_file():
        child_env["XAUTHORITY"] = str(xauthority)

    return {
        "user_name": pw_entry.pw_name,
        "uid": uid,
        "gid": gid,
        "home": home,
        "env": child_env,
    }


def _prepare_q2rtx_subprocess_env(
    runtime_env: dict[str, str],
) -> tuple[dict[str, str], Callable[[], None] | None, str | None]:
    identity = _resolve_q2rtx_run_identity()
    if identity is None:
        return dict(runtime_env), None, None

    child_env = dict(runtime_env)
    child_env.update(identity["env"])

    uid = int(identity["uid"])
    gid = int(identity["gid"])
    user_name = str(identity["user_name"])

    def _drop_privileges() -> None:
        os.initgroups(user_name, gid)
        os.setgid(gid)
        os.setuid(uid)

    return child_env, _drop_privileges, user_name
