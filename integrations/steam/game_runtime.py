"""Wrapper-side per-game profile apply.

Runs inside the PENGUIN_BURNER launch wrapper, before it execs the game:
resolve the launching game (``SteamAppId``) and account (``SteamUser``) to
the stored preset, then ask the root daemon to apply it and watch this PID —
the wrapper's ``exec`` makes it the game session's PID, so the daemon can
restore the standing profile when the game exits. Everything here soft-fails:
a daemon problem must never block a game launch.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys

from profiles.uv.profile_store import read_auto_uv_profiles
from profiles.uv.profile_tiers import resolve_profile_tier_profiles

from .settings import (
    GAME_MODE_ADAPTIVE,
    GAME_MODE_STOCK,
    SteamGameSetting,
    steam_game_setting,
)
from .users import list_steam_users


APP_ID_ENV_VARS = ("SteamAppId", "STEAM_COMPAT_APP_ID", "SteamGameId")
ACCOUNT_NAME_ENV_VARS = ("SteamUser", "SteamAppUser")


def game_app_id(env: dict[str, str]) -> str:
    for key in APP_ID_ENV_VARS:
        value = str(env.get(key) or "").strip()
        if value.isdigit():
            return value
    return ""


def game_account_id(env: dict[str, str], *, home: Path | None = None) -> str:
    """The launching Steam account: match the login name Steam puts in env."""
    users = list_steam_users(home)
    for key in ACCOUNT_NAME_ENV_VARS:
        name = str(env.get(key) or "").strip()
        if not name:
            continue
        for user in users:
            if user.account_name == name:
                return user.account_id
    return users[0].account_id if users else ""


def profile_argv_for_setting(setting: SteamGameSetting) -> list[str] | None:
    """Daemon runtime argv for a preset; None when there is nothing to apply."""
    if setting.mode == GAME_MODE_STOCK:
        return None
    if setting.mode == GAME_MODE_ADAPTIVE:
        # Same shape the UI's "Apply Adaptive" uses: newest profile as the
        # starting point, tier switching driven by present-frame pacing.
        return ["--auto-uv-profile", "latest", "--adaptive-auto-uv"]
    resolved = resolve_profile_tier_profiles(read_auto_uv_profiles())
    profile = resolved.get(setting.mode)
    profile_id = (
        str(profile.get("profile_id") or "").strip()
        if isinstance(profile, dict)
        else ""
    )
    if not profile_id:
        return None
    return ["--auto-uv-profile", profile_id]


def game_runtime_profile_argv(
    env: dict[str, str],
    *,
    home: Path | None = None,
    settings_path: str | Path | None = None,
) -> tuple[list[str], str] | None:
    app_id = game_app_id(env)
    if not app_id:
        return None
    account_id = game_account_id(env, home=home)
    if not account_id:
        return None
    setting = steam_game_setting(account_id, app_id, path=settings_path)
    if setting is None:
        return None
    argv = profile_argv_for_setting(setting)
    if argv is None:
        return None
    return argv, app_id


def apply_game_runtime_profile(
    env: dict[str, str],
    *,
    home: Path | None = None,
    settings_path: str | Path | None = None,
) -> bool:
    resolved = game_runtime_profile_argv(env, home=home, settings_path=settings_path)
    if resolved is None:
        return False
    argv, app_id = resolved
    from runtime.daemon_client import start_game_runtime_profile

    try:
        start_game_runtime_profile(
            argv,
            watch_pid=os.getpid(),
            app_id=app_id,
            timeout_s=3.0,
        )
    except Exception as error:
        print(
            f"penguin-burner: per-game profile apply skipped: {error}",
            file=sys.stderr,
        )
        return False
    return True
