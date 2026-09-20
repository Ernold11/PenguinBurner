"""Adapt Heroic's library and wrapperOptions to the shared wrapper manager.

Game-level rows override global defaults; changes apply at the next launch."""

from __future__ import annotations

from pathlib import Path

from integrations.launchers.compatibility import CompatibilityTools
from integrations.launchers.wrapper_manager import (
    CommandWrite,
    EffectiveCommand,
    WrapperManager,
)

from .compatibility import HeroicCompatibility
from .config_store import (
    SOURCE_LABELS,
    HeroicConfigError,
    effective_wrapper_command,
    read_global_entries,
    write_wrapper_command,
)
from .library import InstalledHeroicGame, read_heroic_games
from .paths import game_config_path, heroic_installation, heroic_installed
from .settings import HEROIC_GAME_SETTINGS_STORE


class HeroicIntegrationManager(WrapperManager):
    launcher_id = "heroic"
    display_name = "Heroic"
    source_labels = SOURCE_LABELS

    def __init__(
        self,
        *,
        home: Path | None = None,
        settings_path: str | Path | None = None,
    ):
        super().__init__(HEROIC_GAME_SETTINGS_STORE, settings_path=settings_path)
        self._home = home
        self.compatibility = CompatibilityTools(
            HeroicCompatibility(home),
            guidance="Applies on the next launch. Use Play here to refresh Heroic's saved settings.",
        )
        self._global_entries: list[dict] | None = None

    @property
    def installation(self):
        return heroic_installation(self._home)

    @property
    def available(self) -> bool:
        return heroic_installed(self._home)

    def read_games(self) -> tuple[InstalledHeroicGame, ...]:
        # Read inherited wrappers once per scan, after the shared cache reset.
        self._global_entries = read_global_entries(self._home)
        return read_heroic_games(self._home)

    def read_effective(self, game: InstalledHeroicGame) -> EffectiveCommand:
        return self._resolve(game, game_level=True)

    def read_inherited(self, game: InstalledHeroicGame) -> str:
        return self._resolve(game, game_level=False).value

    def _resolve(
        self, game: InstalledHeroicGame, *, game_level: bool
    ) -> EffectiveCommand:
        try:
            return effective_wrapper_command(
                game.game_id,
                self._home,
                game_level=game_level,
                global_entries=self._global_entries,
            )
        except HeroicConfigError:
            # A malformed config must not take the whole list down; the row
            # still renders and the write path reports the real error.
            return EffectiveCommand("", "")

    def write_block(self, game: InstalledHeroicGame) -> str:
        if game_config_path(game.game_id, self._home) is None:
            return f"{game.display_name} has no Heroic configuration to write."
        return ""

    def write_command(self, game: InstalledHeroicGame, command: str | None) -> CommandWrite:
        try:
            return write_wrapper_command(
                game.game_id, command, self._home, global_entries=self._global_entries
            )
        except (HeroicConfigError, ValueError) as error:
            return CommandWrite(False, "", str(error))

    def _describe(self, game, setting) -> str:
        description = super()._describe(game, setting)
        if setting.enabled:
            description += " Play in Game Library refreshes Heroic's saved launch settings."
        return description
