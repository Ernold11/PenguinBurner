"""Resolve Heroic config, library and artwork paths for native and Flatpak installs.

An explicit home isolates tests from the real installation."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path

from integrations.launchers.host_paths import host_config_home, safe_config_name
from integrations.launchers.host_process import host_has_command

HEROIC_DIRNAME = "heroic"
#: The Flatpak build keeps the same tree under its own per-app config home.
HEROIC_FLATPAK_DIRNAME = (
    Path(".var") / "app" / "com.heroicgameslauncher.hgl" / "config" / HEROIC_DIRNAME
)
CONFIG_FILENAME = "config.json"
GAME_CONFIG_DIRNAME = "GamesConfig"
LIBRARY_CACHE_DIRNAME = "store_cache"
#: One per store backend. Each is read the same way; a missing one is a store
#: the user never signed into.
LIBRARY_CACHE_FILES = (
    "legendary_library.json",
    "gog_library.json",
    "nile_library.json",
)
SIDELOAD_LIBRARY_PATH = Path("sideload_apps") / "library.json"
#: Which games are actually installed, per store backend. Heroic derives the
#: ``is_installed`` flag in the cached libraries from these when it refreshes,
#: so between refreshes only these are right -- a game installed a moment ago
#: is still ``false`` in the cache.
INSTALLED_STORE_PATHS = {
    "legendary": Path("legendaryConfig") / "legendary" / "installed.json",
    "gog": Path("gog_store") / "installed.json",
    "nile": Path("nile_config") / "nile" / "installed.json",
}
TIMESTAMPS_PATH = Path("store") / "timestamp.json"
DOWNLOAD_HISTORY_PATH = Path("store") / "download-manager.json"
IMAGE_CACHE_DIRNAME = "images-cache"
#: What Heroic's library card appends to an Epic art URL before caching it.
ART_CARD_QUERY = "?h=400&resize=1&w=300"
GAME_ICON_DIRNAME = "icons"


def heroic_config_root(home: Path | None = None) -> Path:
    """Heroic's config directory: everything this integration reads.

    The native and Flatpak builds keep the same tree in two places, so both are
    offered. When both have configuration, prefer native only while its
    executable is installed; stale native settings must not shadow Flatpak.
    An explicit ``home`` wins over the environment, so tests are independent of
    whatever XDG variables the host session exports.
    """
    candidates = _config_roots(home)
    configured = [path for path in candidates if (path / CONFIG_FILENAME).is_file()]
    if len(configured) == 2 and not native_heroic_available():
        return configured[1]
    return configured[0] if configured else candidates[0]


@lru_cache(maxsize=1)
def native_heroic_available() -> bool:
    """Cache host discovery across row/path reads; refresh clears it per scan."""
    return host_has_command("heroic")


def _config_roots(home: Path | None) -> tuple[Path, ...]:
    # The Flatpak build's tree hangs off the real home whatever XDG says, so
    # it is a candidate even when the config home has been redirected.
    base = Path(home) if home is not None else Path.home()
    return (host_config_home(home) / HEROIC_DIRNAME, base / HEROIC_FLATPAK_DIRNAME)


def heroic_installed(home: Path | None = None) -> bool:
    """Whether there is a Heroic configuration to read at all.

    The tab uses this to explain itself instead of showing an empty list on a
    machine that simply has no Heroic.
    """
    return (heroic_config_root(home) / CONFIG_FILENAME).is_file()


def global_config_path(home: Path | None = None) -> Path:
    return heroic_config_root(home) / CONFIG_FILENAME


def game_config_path(app_name: str, home: Path | None = None) -> Path | None:
    """The JSON holding one game's settings, or None for an unusable name.

    ``app_name`` comes straight out of a store's library and is spliced into a
    filename, so a value containing a separator is refused rather than allowed
    to escape the config directory.
    """
    name = safe_config_name(app_name)
    return (
        None
        if name is None
        else heroic_config_root(home) / GAME_CONFIG_DIRNAME / f"{name}.json"
    )


def library_cache_paths(home: Path | None = None) -> tuple[Path, ...]:
    """Every store's cached library, whether or not the user has that store."""
    root = heroic_config_root(home)
    cache = root / LIBRARY_CACHE_DIRNAME
    return (*(cache / name for name in LIBRARY_CACHE_FILES), root / SIDELOAD_LIBRARY_PATH)


def installed_store_paths(home: Path | None = None) -> tuple[Path, ...]:
    """Each store backend's record of what it has installed."""
    root = heroic_config_root(home)
    return tuple(root / relative for relative in INSTALLED_STORE_PATHS.values())


def timestamps_path(home: Path | None = None) -> Path:
    """Where Heroic records first played, last played and minutes played."""
    return heroic_config_root(home) / TIMESTAMPS_PATH


def download_history_path(home: Path | None = None) -> Path:
    """Where Heroic records completed installs and queued downloads."""
    return heroic_config_root(home) / DOWNLOAD_HISTORY_PATH


def game_art_path(
    app_name: str,
    art_urls: Sequence[str],
    home: Path | None = None,
) -> Path | None:
    """Artwork Heroic has already downloaded for a game, or None.

    Nothing is fetched here: a library list must not go to the network. Heroic
    names each cached image by the SHA-256 of the URL it requested, and for
    Epic art that request carries the card's size, so the sized spelling is
    tried before the plain one.
    """
    root = heroic_config_root(home)
    name = safe_config_name(app_name)
    for suffix in (".jpg", ".png", ".jpeg") if name else ():
        icon = root / GAME_ICON_DIRNAME / f"{name}{suffix}"
        if icon.is_file():
            return icon
    cache = root / IMAGE_CACHE_DIRNAME
    for url in art_urls:
        if not url:
            continue
        for spelling in (f"{url}{ART_CARD_QUERY}", url):
            candidate = cache / hashlib.sha256(spelling.encode("utf-8")).hexdigest()
            if candidate.is_file():
                return candidate
    return None
