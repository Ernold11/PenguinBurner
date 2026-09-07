"""Per-game PenguinBurner settings for Heroic games.

The record and its JSON store are shared with every other launcher
(``integrations.launchers.game_settings``); only the file name is Heroic's own,
because app names and Lutris ids would otherwise share one map.

Heroic signs into several stores at once, but they are all one person's
library, so there is no account layer here -- unlike Steam.
"""

from __future__ import annotations

from integrations.launchers.game_settings import (
    GameSettingsStore,
    LauncherGameSetting,
)

HEROIC_GAME_SETTINGS_FILENAME = "heroic-game-settings.json"

HeroicGameSetting = LauncherGameSetting

HEROIC_GAME_SETTINGS_STORE = GameSettingsStore(HEROIC_GAME_SETTINGS_FILENAME)

heroic_game_settings_path = HEROIC_GAME_SETTINGS_STORE.path
load_heroic_game_settings = HEROIC_GAME_SETTINGS_STORE.load
heroic_game_setting = HEROIC_GAME_SETTINGS_STORE.get
store_heroic_game_setting = HEROIC_GAME_SETTINGS_STORE.store
