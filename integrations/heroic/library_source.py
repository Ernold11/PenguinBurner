"""Heroic seen through the launcher contract the game library tab speaks.

A thin read-only face over HeroicIntegrationManager, which stays the only thing
that edits a game's wrapperOptions.
"""

from __future__ import annotations

from pathlib import Path

from integrations.launchers.library import (
    FIELD_TEXT,
    GROUP_COMMAND,
    LauncherBulkAction,
    LauncherField,
    LauncherWriteState,
    LibraryGame,
)
from integrations.launchers.wrapper_manager import LauncherGameRow
from overlay.render_api import overlay_support

from .library import InstalledHeroicGame
from .manager import HeroicIntegrationManager
from .paths import heroic_desktop_icon
from .process import (
    heroic_available,
    launch_heroic_game,
    running_heroic_games,
    stop_heroic_game,
)


class HeroicLibrarySource:
    launcher_id = "heroic"
    display_name = "Heroic"
    #: Shipped fallback, used when the machine has no Heroic icon of its own.
    icon_asset = "tab-heroic.png"
    #: Probed during refresh, off the GUI thread, for the reason Lutris is: a
    #: machine can hold the configuration of a Heroic that is no longer
    #: installed, and inside a Flatpak the probe is a flatpak-spawn round-trip.
    can_launch = False

    def __init__(
        self,
        manager: HeroicIntegrationManager | None = None,
        *,
        home: Path | None = None,
        settings_path: str | Path | None = None,
    ) -> None:
        self.manager = manager or HeroicIntegrationManager(
            home=home,
            settings_path=settings_path,
        )
        self._home = home
        self._rows: tuple[LauncherGameRow, ...] = ()

    def desktop_icon(self):
        """Heroic's own installed icon, or None when it has none here."""
        return heroic_desktop_icon(self._home)

    def available(self) -> bool:
        return bool(self.manager.available)

    def refresh(self, *, deep: bool = True) -> None:
        # Heroic keeps everything in local JSON, so there is no expensive pass
        # to skip: the cheap one is the only one.
        del deep
        self.manager.refresh()
        self._rows = tuple(self.manager.rows())
        # Installing or removing Heroic while the tab is open should change the
        # Play button on the next scan, not on the next app start.
        self.can_launch = heroic_available()

    # -- what only Heroic has ------------------------------------------------

    def fields(self, game: LibraryGame) -> tuple[LauncherField, ...]:
        row = game.detail
        if not isinstance(row, LauncherGameRow):
            return ()
        return (self._wrapper_command_field(row),)

    @staticmethod
    def _wrapper_command_field(row: LauncherGameRow) -> LauncherField:
        value = str(row.command or "")
        subtitle = "Wrapper command in the Heroic settings"
        if value and row.inherited:
            subtitle = f"Wrapper command — inherited from {row.source_label}"
        return LauncherField(
            key="wrapper_command",
            kind=FIELD_TEXT,
            title="Command",
            subtitle=subtitle,
            setter="set_game_wrapper_command",
            value=value,
            group=GROUP_COMMAND,
        )

    def write_state(self) -> LauncherWriteState:
        """Always ready: Heroic's settings are files we own the writing of."""
        return LauncherWriteState()

    def bulk_actions(self) -> tuple[LauncherBulkAction, ...]:
        # Keys shared with the other launchers on purpose: the tab shows one
        # "disable everything" and means it across the whole library.
        return (
            LauncherBulkAction(
                key="enable_all",
                label="Enable PenguinBurner for all games",
                setter="set_all_games_enabled",
                value=True,
                affects="enabled",
                confirm=(
                    "Add the PenguinBurner wrapper to the launch command of "
                    "{count} {games}?\n\nThe In-Game overlay stays off, and "
                    "MangoHud is disabled in wrapped games. \"Disable "
                    "PenguinBurner for all games\" restores each game's own "
                    "wrapper command."
                ),
            ),
            LauncherBulkAction(
                key="disable_all",
                label="Disable PenguinBurner for all games",
                setter="set_all_games_enabled",
                value=False,
                affects="enabled",
                confirm=(
                    "Remove the PenguinBurner wrapper from {count} {games} and "
                    "restore their own wrapper command?"
                ),
            ),
        )

    def after_setting_write(self, game_id: str, setter: str) -> None:
        """Heroic changes are picked up on the next launch, not live."""
        del game_id, setter

    def games(self) -> tuple[LibraryGame, ...]:
        return tuple(self._library_game(row) for row in self._rows)

    def _library_game(self, row: LauncherGameRow) -> LibraryGame:
        game: InstalledHeroicGame = row.game
        supported, reason = self._overlay_capability(game)
        return LibraryGame(
            launcher=self.launcher_id,
            game_id=game.game_id,
            name=game.display_name,
            subtitle=game.runner_label,
            last_played=int(game.last_played or 0),
            playtime_hours=float(game.playtime_hours or 0.0),
            art_path=game.art_path,
            ready=bool(game.ready),
            wrapped=bool(row.wrapped),
            enabled=bool(row.setting.enabled),
            overlay=bool(row.setting.overlay),
            detail=row,
            overlay_supported=supported,
            overlay_unsupported_reason=reason,
        )

    @staticmethod
    def _overlay_capability(game: InstalledHeroicGame) -> tuple[bool, str]:
        # Windows games run under Proton, where everything is translated to
        # Vulkan and the overlay always reaches them. Only a Linux-native
        # build can be an OpenGL program the overlay cannot draw in.
        if not game.is_native:
            return True, ""
        return overlay_support(
            translated_to_vulkan=False,
            executable=None,
            directory=game.install_path or None,
        )

    # -- launching -------------------------------------------------------------

    def launch(self, game_id: str) -> tuple[bool, str]:
        """Ask Heroic to start a game. Returns (started, what to tell the user)."""
        if not self.can_launch:
            return False, "FAILED to launch (heroic not on PATH)"
        row = self.manager.row(game_id)
        if row is None:
            return False, "FAILED to launch (no such game in the Heroic library)"
        if launch_heroic_game(row.game.runner, row.game.game_id):
            return True, "launching via Heroic…"
        return False, "FAILED to launch (heroic would not start the game)"

    def stop(self, game_id: str) -> tuple[bool, str]:
        """Signal the game's PenguinBurner wrapper, which is the session itself."""
        running = running_heroic_games()
        if running is None:
            return False, "FAILED to stop (could not tell what is running)"
        pids = running.get(str(game_id), ())
        if not pids:
            return False, "FAILED to stop (no running session for this game)"
        if stop_heroic_game(pids[0]):
            return True, "stopping…"
        return False, "FAILED to stop (the wrapper would not take the signal)"

    def running_game_ids(self) -> frozenset[str] | None:
        """Which of this launcher's games are running, or None if unknowable.

        Only games PenguinBurner wraps can be seen: their command line carries
        the identity flag we wrote. A game Heroic starts untouched is invisible
        here, which is also a game this tab has nothing to say about.
        """
        running = running_heroic_games()
        return None if running is None else frozenset(running)
