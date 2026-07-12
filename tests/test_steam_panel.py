from __future__ import annotations

import os
from pathlib import Path
import threading
from types import SimpleNamespace
from typing import cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets

from integrations.steam.library import InstalledSteamGame
from integrations.steam.manager import SteamGameRow, SteamIntegrationManager
from integrations.steam.settings import (
    GAME_MODE_DEFAULT,
    GAME_MODE_STOCK,
    SteamGameSetting,
)
from ui.components.steam_panel import (
    SORT_ALPHABETICAL,
    SORT_RECENT,
    SteamPanel,
    _mode_keys_for_standing_mode,
    last_played_text,
    sorted_steam_rows,
)
from ui.qt import import_qt


def _row(
    tmp_path: Path,
    app_id: str,
    name: str,
    last_played: int,
    *,
    command: str = "%command%",
) -> SteamGameRow:
    return SteamGameRow(
        game=InstalledSteamGame(
            app_id=app_id,
            name=name,
            install_dir=name,
            steamapps_dir=tmp_path / "steamapps",
            state_flags=4,
            last_played=last_played,
            icon_path=None,
            compat_tool="proton_experimental",
        ),
        setting=SteamGameSetting(),
        launch_options=command,
    )


class _FakeManager:
    def __init__(self, rows: tuple[SteamGameRow, ...]) -> None:
        self.rows = rows

    def refresh(self, *, read_launch_options: bool = True):
        return self.rows

    def standing_mode_label(self) -> str:
        return "Balanced"

    def active_user(self):
        return SimpleNamespace(display_name="jan.pietek")

    def marker_present(self) -> bool:
        return True

    def steam_running(self) -> bool:
        return True

    def cdp_ready(self) -> bool:
        return True


class _RunningThread(threading.Thread):
    def is_alive(self) -> bool:
        return True


class _NoRefreshManager(SteamIntegrationManager):
    def refresh(self, **kwargs):
        raise AssertionError("auto-sync must not race the full rescan")


def test_recent_sort_uses_last_played_and_puts_never_played_last(
    tmp_path: Path,
) -> None:
    rows = (
        _row(tmp_path, "10", "Alpha", 100),
        _row(tmp_path, "20", "Beta", 0),
        _row(tmp_path, "30", "Zeta", 300),
    )

    assert [row.game.app_id for row in sorted_steam_rows(rows, SORT_RECENT)] == [
        "30",
        "10",
        "20",
    ]
    assert [
        row.game.app_id for row in sorted_steam_rows(rows, SORT_ALPHABETICAL)
    ] == ["10", "20", "30"]
    assert last_played_text(0) == "Never played on this PC"


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
    panel.manager = _NoRefreshManager()

    panel._auto_sync()


def test_panel_keeps_library_left_and_one_selected_game_editor(
    qtbot,
    tmp_path: Path,
) -> None:
    QtCore, QtGui, QtWidgetsModule, _pg = import_qt()
    rows = (
        _row(tmp_path, "10", "Alpha", 100, command="alpha %command%"),
        _row(tmp_path, "20", "Beta", 0, command="beta %command%"),
        _row(tmp_path, "30", "Zeta", 300, command="zeta %command%"),
    )
    panel = SteamPanel(
        QtCore=QtCore,
        QtGui=QtGui,
        QtWidgets=QtWidgetsModule,
        manager=cast(SteamIntegrationManager, _FakeManager(rows)),
    )
    qtbot.addWidget(panel.widget)
    qtbot.waitUntil(lambda: not panel._scan_running, timeout=2000)

    assert panel.splitter.indexOf(panel.library_pane) == 0
    assert panel.splitter.indexOf(panel.details_pane) == 1
    assert not panel.splitter.isCollapsible(0)
    assert panel.sort_combo.currentData() == SORT_RECENT
    assert [panel.game_list.item(i).text() for i in range(3)] == [
        "Zeta",
        "Alpha",
        "Beta",
    ]
    assert panel.game_title.text() == "Zeta"
    assert panel.launch_edit.text() == "zeta %command%"

    line_edits = panel.widget.findChildren(QtWidgets.QLineEdit)
    tables = panel.widget.findChildren(QtWidgets.QTableWidget)
    play_buttons = [
        button
        for button in panel.widget.findChildren(QtWidgets.QPushButton)
        if button.text() == "Play"
    ]
    reset_buttons = [
        button
        for button in panel.widget.findChildren(QtWidgets.QPushButton)
        if button.text() == "Reset"
    ]
    assert line_edits == [panel.launch_edit]
    assert tables == []
    assert play_buttons == [panel.play_button]
    assert reset_buttons == []

    panel.sort_combo.setCurrentIndex(panel.sort_combo.findData(SORT_ALPHABETICAL))
    assert [panel.game_list.item(i).text() for i in range(3)] == [
        "Alpha",
        "Beta",
        "Zeta",
    ]
    # Sorting changes position, not the selected game or its editor state.
    assert panel.game_title.text() == "Zeta"
    assert panel.launch_edit.text() == "zeta %command%"

    panel._sync_timer.stop()
