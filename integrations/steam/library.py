"""Installed-game scan: manifests, install state, icons, Proton mapping."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from overlay.telemetry.steam_game_setup import (
    EXCLUDED_INSTALLED_APP_NAME_PREFIXES,
    default_steamapps_dirs,
)

from .users import default_steam_root
from .vdf import parse_vdf, vdf_lookup


# StateFlags is a bitfield; mid-update values like 6 must still count as
# installed, so test bits instead of comparing whole values.
STATE_FLAG_FULLY_INSTALLED = 4
STATE_FLAG_UNINSTALLING = 1

_ICON_MAX_BYTES = 65536


@dataclass(frozen=True)
class InstalledSteamGame:
    app_id: str
    name: str
    install_dir: str
    steamapps_dir: Path
    state_flags: int
    icon_path: Path | None
    compat_tool: str

    @property
    def ready(self) -> bool:
        return bool(self.state_flags & STATE_FLAG_FULLY_INSTALLED) and not bool(
            self.state_flags & STATE_FLAG_UNINSTALLING
        )

    @property
    def is_proton(self) -> bool:
        return bool(self.compat_tool)


def installed_steam_games(home: Path | None = None) -> tuple[InstalledSteamGame, ...]:
    """Ready-to-play installed games across all Steam libraries, sorted by name."""
    root = default_steam_root(home)
    compat_tools = _compat_tool_mapping(root)
    games: list[InstalledSteamGame] = []
    seen_app_ids: set[str] = set()
    for steamapps_dir in default_steamapps_dirs(home):
        for manifest in sorted(steamapps_dir.glob("appmanifest_*.acf")):
            game = _game_from_manifest(
                manifest,
                steamapps_dir=steamapps_dir,
                steam_root=root,
                compat_tools=compat_tools,
            )
            if game is None or game.app_id in seen_app_ids:
                continue
            seen_app_ids.add(game.app_id)
            if game.ready:
                games.append(game)
    games.sort(key=lambda game: game.name.casefold())
    return tuple(games)


def _game_from_manifest(
    manifest: Path,
    *,
    steamapps_dir: Path,
    steam_root: Path | None,
    compat_tools: dict[str, str],
) -> InstalledSteamGame | None:
    try:
        fields = parse_vdf(manifest.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    state = vdf_lookup(fields, "AppState")
    if not isinstance(state, dict):
        return None
    # The filename is authoritative for the app id; the field is a fallback.
    app_id = manifest.stem.removeprefix("appmanifest_").strip()
    if not app_id.isdigit():
        app_id = str(vdf_lookup(state, "appid") or "").strip()
    name = str(vdf_lookup(state, "name") or "").strip()
    if not app_id or not name or name.startswith(EXCLUDED_INSTALLED_APP_NAME_PREFIXES):
        return None
    try:
        state_flags = int(str(vdf_lookup(state, "StateFlags") or 0))
    except ValueError:
        state_flags = 0
    return InstalledSteamGame(
        app_id=app_id,
        name=name,
        install_dir=str(vdf_lookup(state, "installdir") or "").strip(),
        steamapps_dir=steamapps_dir,
        state_flags=state_flags,
        icon_path=game_icon_path(app_id, steam_root=steam_root),
        compat_tool=compat_tools.get(app_id, ""),
    )


def game_icon_path(app_id: str, *, steam_root: Path | None) -> Path | None:
    """Smallest library-cache JPEG for the app — Steam's 32x32 client icon."""
    if steam_root is None:
        return None
    cache_dir = steam_root / "appcache" / "librarycache"
    legacy = cache_dir / f"{app_id}_icon.jpg"
    if legacy.is_file():
        return legacy
    candidates: list[tuple[int, Path]] = []
    try:
        for path in (cache_dir / app_id).iterdir():
            if path.suffix.lower() != ".jpg" or not path.is_file():
                continue
            size = path.stat().st_size
            if 0 < size <= _ICON_MAX_BYTES:
                candidates.append((size, path))
    except OSError:
        return None
    return min(candidates)[1] if candidates else None


def _compat_tool_mapping(steam_root: Path | None) -> dict[str, str]:
    """appid -> explicitly assigned compat tool name from config.vdf."""
    if steam_root is None:
        return {}
    try:
        text = (steam_root / "config" / "config.vdf").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return {}
    mapping = vdf_lookup(
        parse_vdf(text),
        "InstallConfigStore",
        "Software",
        "Valve",
        "Steam",
        "CompatToolMapping",
    )
    if not isinstance(mapping, dict):
        return {}
    tools: dict[str, str] = {}
    for app_id, entry in mapping.items():
        name = str(vdf_lookup(entry, "name") or "").strip()
        if name:
            tools[str(app_id)] = name
    return tools
