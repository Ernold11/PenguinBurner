"""Steam account discovery from loginusers.vdf.

Steam stores per-account launch options under ``userdata/<accountid>/``, so
every localconfig read/write must be scoped to one account. The CDP live-apply
path always operates on the account currently logged into the Steam client,
which loginusers.vdf tracks via ``MostRecent``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .vdf import parse_vdf, vdf_lookup


STEAMID64_BASE = 76561197960265728


@dataclass(frozen=True)
class SteamUser:
    account_id: str
    steamid64: str
    account_name: str
    persona_name: str
    most_recent: bool
    timestamp: int
    userdata_dir: Path

    @property
    def display_name(self) -> str:
        return self.persona_name or self.account_name or self.account_id

    @property
    def localconfig_path(self) -> Path:
        return self.userdata_dir / "config" / "localconfig.vdf"


def default_steam_root(home: Path | None = None) -> Path | None:
    home = Path.home() if home is None else home
    for candidate in (
        home / ".local" / "share" / "Steam",
        home / ".steam" / "steam",
        home / ".steam" / "root",
    ):
        if (candidate / "steamapps").is_dir() or (candidate / "userdata").is_dir():
            return candidate
    return None


def list_steam_users(home: Path | None = None) -> tuple[SteamUser, ...]:
    """Accounts from loginusers.vdf that have a userdata dir, active first."""
    root = default_steam_root(home)
    if root is None:
        return ()
    try:
        text = (root / "config" / "loginusers.vdf").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return ()
    entries = vdf_lookup(parse_vdf(text), "users")
    if not isinstance(entries, dict):
        return ()

    users: list[SteamUser] = []
    for steamid64, info in entries.items():
        if not isinstance(info, dict):
            continue
        try:
            account_id = int(str(steamid64)) - STEAMID64_BASE
        except ValueError:
            continue
        if account_id <= 0:
            continue
        userdata_dir = root / "userdata" / str(account_id)
        if not userdata_dir.is_dir():
            continue
        try:
            timestamp = int(str(vdf_lookup(info, "Timestamp") or 0))
        except ValueError:
            timestamp = 0
        users.append(
            SteamUser(
                account_id=str(account_id),
                steamid64=str(steamid64),
                account_name=str(vdf_lookup(info, "AccountName") or ""),
                persona_name=str(vdf_lookup(info, "PersonaName") or ""),
                most_recent=str(vdf_lookup(info, "MostRecent") or "") == "1",
                timestamp=timestamp,
                userdata_dir=userdata_dir,
            )
        )
    users.sort(key=lambda user: (not user.most_recent, -user.timestamp))
    return tuple(users)


def active_steam_user(home: Path | None = None) -> SteamUser | None:
    """The logged-in account (MostRecent, falling back to newest login)."""
    users = list_steam_users(home)
    return users[0] if users else None
