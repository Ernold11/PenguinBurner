"""Read the Heroic library out of its per-store cache files.

Heroic keeps one cached library per store backend -- Epic through legendary,
GOG through gogdl, Amazon through nile, plus games the user sideloaded -- and
they all describe a game the same way, so one reader answers for all four. A
store the user never signed into is simply a file that is not there.

Playtime and last-played live apart from the library, in ``store/timestamp.json``,
because Heroic writes them when a session ends rather than when it syncs.

Every failure degrades to an empty library: a half-written cache, an older
schema, or a file we cannot read must leave the tab empty and explaining
itself, never raise into the GUI.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .paths import game_art_path, library_cache_paths, timestamps_path

#: Both spellings a cache file uses for its list of games.
_GAME_LIST_KEYS = ("library", "games")


@dataclass(frozen=True)
class InstalledHeroicGame:
    game_id: str  # Heroic's app_name: an Epic hash, a GOG id, an Amazon ASIN
    name: str
    runner: str  # legendary, gog, nile, sideload
    installed: bool
    platform: str
    install_path: str
    last_played: int
    playtime_hours: float
    art_path: Path | None

    @property
    def ready(self) -> bool:
        """Configurable: installed, which is all a wrapper entry needs."""
        return self.installed

    @property
    def display_name(self) -> str:
        return self.name or self.game_id

    @property
    def runner_label(self) -> str:
        """The store, as Heroic's own UI names it."""
        return _RUNNER_LABELS.get(self.runner, self.runner or "unknown")

    @property
    def is_native(self) -> bool:
        """Linux-native games are the ones running as plain ELF programs."""
        return self.platform.lower() in ("linux", "native")


_RUNNER_LABELS = {
    "legendary": "Epic",
    "gog": "GOG",
    "nile": "Amazon",
    "sideload": "Sideloaded",
}


def read_heroic_games(
    home: Path | None = None,
    *,
    include_uninstalled: bool = False,
) -> tuple[InstalledHeroicGame, ...]:
    """Every game Heroic knows about, newest-played first.

    Uninstalled entries are dropped by default: they have no launch command to
    wrap and would only pad the list. So are DLC, which Heroic lists beside
    their game but never launches.
    """
    timestamps = _read_timestamps(timestamps_path(home))
    games: list[InstalledHeroicGame] = []
    seen: set[str] = set()
    for path in library_cache_paths(home):
        for entry in _entries(path):
            # Filtered before the record is built: a store's cache lists
            # everything the account owns, and resolving artwork for hundreds
            # of games nobody installed is a stat storm per library scan.
            if not include_uninstalled and not entry.get("is_installed"):
                continue
            game = _game_from_entry(entry, timestamps, home)
            if game is None or game.game_id in seen:
                continue
            seen.add(game.game_id)
            games.append(game)
    games.sort(key=lambda g: (-int(g.last_played or 0), g.display_name.casefold()))
    return tuple(games)


def heroic_game(
    game_id: str,
    home: Path | None = None,
) -> InstalledHeroicGame | None:
    wanted = str(game_id or "").strip()
    if not wanted:
        return None
    for game in read_heroic_games(home, include_uninstalled=True):
        if game.game_id == wanted:
            return game
    return None


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        # A store that was never signed into, or a cache Heroic is rewriting
        # right now. Either way there is nothing to list from it.
        return None


def _entries(path: Path) -> list[dict]:
    payload = _read_json(path)
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = next(
            (payload[key] for key in _GAME_LIST_KEYS if isinstance(payload.get(key), list)),
            [],
        )
    else:
        rows = []
    return [row for row in rows if isinstance(row, dict)]


def _read_timestamps(path: Path) -> dict[str, tuple[int, float]]:
    """app_name -> (last played epoch seconds, hours played)."""
    payload = _read_json(path)
    if not isinstance(payload, dict):
        return {}
    stamps: dict[str, tuple[int, float]] = {}
    for app_name, entry in payload.items():
        if not isinstance(entry, dict):
            continue
        # Heroic counts minutes; the library compares hours across launchers.
        minutes = _float(entry.get("totalPlayed"))
        stamps[str(app_name)] = (_epoch(entry.get("lastPlayed")), minutes / 60.0)
    return stamps


def _game_from_entry(
    entry: dict,
    timestamps: dict[str, tuple[int, float]],
    home: Path | None,
) -> InstalledHeroicGame | None:
    game_id = str(entry.get("app_name") or "").strip()
    if not game_id:
        return None
    install = entry.get("install")
    install = install if isinstance(install, dict) else {}
    if bool(install.get("is_dlc")):
        # Listed beside its game, never launched on its own.
        return None
    last_played, playtime_hours = timestamps.get(game_id, (0, 0.0))
    return InstalledHeroicGame(
        game_id=game_id,
        name=str(entry.get("title") or "").strip(),
        runner=str(entry.get("runner") or "").strip(),
        installed=bool(entry.get("is_installed")),
        platform=str(install.get("platform") or "").strip()
        or ("linux" if entry.get("is_linux_native") else ""),
        install_path=str(install.get("install_path") or "").strip(),
        last_played=last_played,
        playtime_hours=playtime_hours,
        art_path=game_art_path(
            game_id,
            (str(entry.get("art_square") or ""), str(entry.get("art_cover") or "")),
            home,
        ),
    )


def _epoch(value: object) -> int:
    """Heroic's ISO timestamp as epoch seconds; 0 when it never recorded one."""
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return 0
    return max(int(moment.timestamp()), 0)


def _float(value: object) -> float:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return max(result, 0.0)
