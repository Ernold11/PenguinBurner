from integrations.steam.settings import GAME_MODE_DEFAULT, GAME_MODE_STOCK
from ui.components.steam_panel import SteamPanel, _mode_keys_for_standing_mode


class _RunningThread:
    def is_alive(self) -> bool:
        return True


class _Manager:
    def refresh(self, **kwargs):
        raise AssertionError("auto-sync must not race the full rescan")


def test_table_has_no_duplicate_reset_action() -> None:
    assert SteamPanel.COLUMNS == [
        "Game",
        "Auto UV",
        "Overlay",
        "Launch options",
        "",
    ]


def test_stock_standing_mode_is_only_listed_once_at_the_top() -> None:
    mode_keys = _mode_keys_for_standing_mode("Stock")

    assert mode_keys[0] == GAME_MODE_DEFAULT
    assert GAME_MODE_STOCK not in mode_keys


def test_explicit_stock_remains_available_when_default_is_not_stock() -> None:
    mode_keys = _mode_keys_for_standing_mode("Balanced")

    assert mode_keys[0] == GAME_MODE_DEFAULT
    assert mode_keys[-1] == GAME_MODE_STOCK


def test_auto_sync_skips_while_full_rescan_is_running() -> None:
    panel = object.__new__(SteamPanel)
    panel._rescan_thread = _RunningThread()
    panel.manager = _Manager()

    panel._auto_sync()
