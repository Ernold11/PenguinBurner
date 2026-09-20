"""Heroic's library adapter; settings writes belong to HeroicIntegrationManager."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from integrations.launchers.library import LauncherField, LibraryGame
from integrations.launchers.library_source import WrapperLibrarySource
from integrations.launchers.wrapper_manager import ApplyResult, LauncherGameRow
from overlay.render_api import overlay_support

from .flatpak import uses_flatpak, wrapper_path
from .manager import HeroicIntegrationManager
from .process import (
    HeroicSessions,
    heroic_available,
    launch_heroic_game,
    probe_heroic_sessions,
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
    _running_pids: tuple[int, ...] = ()
    _external_games: frozenset[str] = frozenset()

    def watch_paths(self) -> tuple[Path, ...]:
        from .paths import (
            heroic_config_root,
            installed_store_paths,
            library_cache_paths,
        )

        root = heroic_config_root(self._home)
        return (*installed_store_paths(self._home), *library_cache_paths(self._home),
                root / "store", root / "GamesConfig", root / "config.json")

    def build_manager(self, *, home, settings_path) -> HeroicIntegrationManager:
        return HeroicIntegrationManager(home=home, settings_path=settings_path)

    def probe_can_launch(self) -> bool:
        return heroic_available(self._home)

    def fields(self, game: LibraryGame) -> tuple[LauncherField, ...]:
        fields = super().fields(game)
        if not game.wrapped or not uses_flatpak(self._home):
            return fields
        wrapper = wrapper_path(self._home)
        row = game.detail
        ready = (
            isinstance(row, LauncherGameRow)
            and str(wrapper) in row.command
            and wrapper.is_file()
        )
        note = (
            "Configured for Flatpak; Play refreshes Heroic's saved launch settings."
            if ready else
            "Flatpak setup required: turn Wrap this game off and on, then use Play."
        )
        return tuple(
            replace(field, subtitle=note) if field.key == self.command_field_key else field
            for field in fields
        )

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

    def after_setting_write(self, game_id: str, setter: str) -> ApplyResult | None:
        """Deliver targets to the daemon and visibility to the loaded layer."""
        if setter != "set_game_overlay":
            return super().after_setting_write(game_id, setter)
        return self._reapply_overlay((game_id,))

    def after_bulk_write(self, setter: str, result: ApplyResult) -> ApplyResult | None:
        if setter == "set_all_games_overlay":
            return self._reapply_overlay(result.applied_game_ids)
        return None

    def _reapply_overlay(self, game_ids: tuple[str, ...]) -> ApplyResult | None:
        if not game_ids:
            return None
        running = self._running_sessions()
        if running is None:
            return ApplyResult(
                False, "Overlay saved, but the running game could not be checked for a live update."
            )
        wrapped = set(game_ids).intersection(running.wrapped)
        external = set(game_ids).intersection(running.external)
        messages: list[str] = []
        if wrapped:
            from overlay.state import write_overlay_override

            row = self.manager.row(next(iter(wrapped)))
            if row is None or not write_overlay_override(row.setting.overlay):
                return ApplyResult(False, "Overlay saved, but live visibility update failed.")
            messages.append(
                f"Overlay switched {'on' if row.setting.overlay else 'off'} live (within one second)."
            )
        if external:
            messages.append(
                "Overlay saved; a game started without PenguinBurner. "
                "Close the game, then relaunch with Play in Game Library. "
                "Overlay visibility can change live after that."
            )
        return ApplyResult(not external, " ".join(messages)) if messages else None

    def launch(self, game_id: str) -> tuple[bool, str]:
        """Ask Heroic to start a game. Returns (started, what to tell the user)."""
        if not self.can_launch:
            return False, "FAILED to launch (Heroic is not installed or could not be found)"
        row = self.manager.row(game_id)
        if row is None:
            return False, "FAILED to launch (no such game in the Heroic library)"
        if launch_heroic_game(row.game.runner, row.game.game_id, home=self._home):
            return True, "launching via Heroic…"
        return False, "FAILED to launch (heroic would not start the game)"

    def stop(self, game_id: str) -> tuple[bool, str]:
        """Signal the wrapped game's surviving session members."""
        running = self._running_sessions()
        if running is None:
            return False, "FAILED to stop (could not tell what is running)"
        pids = running.wrapped.get(str(game_id), ())
        if not pids:
            return False, "FAILED to stop (no running session for this game)"
        # Handoffs can leave several identified successors. Stop every wrapped
        # member, never an external launcher process or detached PB helper.
        stopped = [stop_heroic_game(pid, str(game_id)) for pid in pids]
        if all(stopped):
            return True, "stopping…"
        return False, "FAILED to stop (the wrapper would not take the signal)"

    def running_game_ids(self) -> frozenset[str] | None:
        """Which of this launcher's games are running, or None if unknowable.

        Heroic's own launch children remain observable without our wrapper.
        They are kept separate from the wrapper sessions Stop can control.
        """
        running = self._running_sessions()
        return None if running is None else frozenset(running.wrapped) | frozenset(running.external)

    def external_game_ids(self) -> frozenset[str]:
        """Observed games that must be closed in Heroic, from the latest poll."""
        return self._external_games

    def observed_processes(self) -> dict[str, tuple[int, ...]] | None:
        sessions = self._running_sessions()
        return None if sessions is None else sessions.external

    def _running_sessions(self) -> HeroicSessions | None:
        running = probe_heroic_sessions(known_pids=self._running_pids)
        if running is not None:
            self._running_pids = tuple(
                pid for group in (running.wrapped, running.external)
                for pids in group.values() for pid in pids
            )
            self._external_games = frozenset(running.external) - frozenset(running.wrapped)
        return running
