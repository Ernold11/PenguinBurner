"""Where Faugus Launcher keeps the two files PenguinBurner reads.

Faugus splits its state the XDG way: preferences under the config home, the
library under the data home. Both are read straight from disk, because Faugus
rewrites them on save rather than holding a lock we could wait on.

Which installation those hang off -- native or Flatpak -- is the shared
question every launcher answers through ``select_installation``, so discovery,
writes and launching all land on the same Faugus.
"""

from __future__ import annotations

from pathlib import Path

from integrations.launchers.host_paths import host_config_home, host_data_home
from integrations.launchers.installation import (
    LauncherInstallation,
    select_installation,
)

FAUGUS_DIRNAME = "faugus-launcher"
FAUGUS_COMMAND = "faugus-launcher"
FAUGUS_FLATPAK_APP_ID = "io.github.Faugus.faugus-launcher"
CONFIG_FILENAME = "config.json"
GAMES_FILENAME = "games.json"


def faugus_installation(home: Path | None = None) -> LauncherInstallation:
    base = home or Path.home()
    return select_installation(
        FAUGUS_COMMAND, FAUGUS_FLATPAK_APP_ID, home=home,
        native_roots=(host_data_home(home) / FAUGUS_DIRNAME,),
        flatpak_roots=(
            base / ".var/app" / FAUGUS_FLATPAK_APP_ID / "data" / FAUGUS_DIRNAME,
        ),
        markers=(GAMES_FILENAME,),
    )


def games_path(home: Path | None = None) -> Path:
    """Faugus's library file, in the installation this machine actually uses."""
    return faugus_installation(home).root / GAMES_FILENAME


def config_path(home: Path | None = None) -> Path:
    """Faugus's preferences, beside the library rather than across installations.

    The Flatpak build resolves XDG inside its own sandbox, so its config home
    is the app's, not the host's.
    """
    installation = faugus_installation(home)
    base = (
        (installation.app_home / "config")
        if installation.flatpak
        else host_config_home(home)
    )
    return base / FAUGUS_DIRNAME / CONFIG_FILENAME


def faugus_installed(home: Path | None = None) -> bool:
    """Whether there is a Faugus library to read at all.

    The library is the file that matters: a config.json alone describes a
    Faugus nobody has added a game to, and there is nothing there to wrap.
    """
    return games_path(home).is_file()
