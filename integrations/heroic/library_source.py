"""Heroic's library adapter; settings writes belong to HeroicIntegrationManager."""

from __future__ import annotations

from integrations.launchers.library_source import WrapperLibrarySource
from integrations.launchers.wrapper_manager import LauncherGameRow
from overlay.render_api import overlay_support

from .manager import HeroicIntegrationManager
from .process import (
    heroic_available,
    launch_heroic_game,
    running_heroic_games,
    stop_heroic_game,
)


class HeroicLibrarySource(WrapperLibrarySource):
    launcher_id = "heroic"
    display_name = "Heroic"
    icon_asset = "tab-heroic.png"
    #: The distro builds ship a plain "heroic", the Flatpak its application id.
    desktop_icon_names = ("heroic", "com.heroicgameslauncher.hgl")

    command_field_key = "wrapper_command"
    command_field_subtitle = "Wrapper command in the Heroic settings"
    command_field_inherited_subtitle = "Wrapper command — inherited from {source}"
    command_noun = "wrapper command"
    _running_pids: tuple[int, ...] = ()

    def build_manager(self, *, home, settings_path) -> HeroicIntegrationManager:
        return HeroicIntegrationManager(home=home, settings_path=settings_path)

    def probe_can_launch(self) -> bool:
        return heroic_available()

    def overlay_capability(self, row: LauncherGameRow) -> tuple[bool, str]:
        game = row.game
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
            return False, "FAILED to launch (Heroic is not installed or could not be found)"
        row = self.manager.row(game_id)
        if row is None:
            return False, "FAILED to launch (no such game in the Heroic library)"
        if launch_heroic_game(row.game.runner, row.game.game_id):
            return True, "launching via Heroic…"
        return False, "FAILED to launch (heroic would not start the game)"

    def stop(self, game_id: str) -> tuple[bool, str]:
        """Signal the game's PenguinBurner wrapper, which is the session itself."""
        running = self._running_sessions()
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

        Only wrapped games carry our session identity in their environment.
        The session PID excludes helper processes that inherit the same key.
        """
        running = self._running_sessions()
        return None if running is None else frozenset(running)

    def _running_sessions(self) -> dict[str, tuple[int, ...]] | None:
        running = running_heroic_games(known_pids=self._running_pids)
        if running is not None:
            self._running_pids = tuple(pid for pids in running.values() for pid in pids)
        return running
