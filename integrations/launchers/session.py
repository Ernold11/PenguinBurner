"""Wrapper-side lifecycle reporting. Observation never prevents a game launch."""

from __future__ import annotations

import uuid

from overlay.wrapper_tokens import GAME_KEY_ENV
from runtime.daemon_client import register_launcher_session, update_launcher_session

SESSION_ID_ENV = "PENGUIN_BURNER_SESSION_ID"


def begin_session(env: dict[str, str]) -> tuple[str, int | None]:
    """Give every launch a new identity, including a game with no GPU preset."""
    key = env.get(GAME_KEY_ENV, "")
    if not key:
        from integrations.steam.game_runtime import game_app_id

        app_id = game_app_id(env)
        key = f"steam:{app_id}" if app_id else ""
    if not key:
        return "", None
    env[GAME_KEY_ENV] = key
    session_id = uuid.uuid4().hex
    env[SESSION_ID_ENV] = session_id
    try:
        result = register_launcher_session(key, session_id)
        host_pid = int(result["pid"])
        if host_pid <= 0:
            raise ValueError("invalid session host PID")
        env["PENGUIN_BURNER_TELEMETRY_SESSION"] = str(host_pid)
        return session_id, host_pid
    except (RuntimeError, OSError, ValueError, KeyError):
        # Keep identity in the exec environment for daemon-restart recovery.
        # Do not retry GPU application later as a side effect of reconnecting.
        pass
    return session_id, None


def report_session(session_id: str, *, phase: str, profile_applied: bool = False) -> None:
    if not session_id:
        return
    try:
        update_launcher_session(
            session_id, phase=phase,
            profile="applied" if profile_applied else "unconfirmed",
        )
    except (RuntimeError, OSError, ValueError):
        pass
