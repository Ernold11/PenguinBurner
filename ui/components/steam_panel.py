"""Steam tab: per-game Auto-UV mode, overlay toggle, and launch options.

Thin view over integrations.steam.manager — every edit applies immediately
(live via CDP while Steam runs) and persists per Steam account. The table
lists installed games only, kept in sync with the library by a background
rescan timer.
"""

from __future__ import annotations

import textwrap
import threading

from integrations.steam.manager import SteamGameRow, SteamIntegrationManager
from integrations.steam.process import launch_steam_game, restart_steam
from integrations.steam.settings import (
    GAME_MODE_ADAPTIVE,
    GAME_MODE_DEFAULT,
    GAME_MODE_STOCK,
    normalize_game_mode,
)
from profiles.uv.profile_tiers import PROFILE_TIER_LABELS, PROFILE_TIERS


# "Default" (no per-game choice, follows the Profiles-tab standing action)
# leads; explicit Stock is last unless the standing action is already Stock.
_MODE_LABELS = {
    GAME_MODE_DEFAULT: "Default",
    GAME_MODE_ADAPTIVE: "Adaptive",
    **PROFILE_TIER_LABELS,
    GAME_MODE_STOCK: "Stock",
}
_MODE_KEYS = (GAME_MODE_DEFAULT, GAME_MODE_ADAPTIVE, *PROFILE_TIERS, GAME_MODE_STOCK)

_AUTO_SYNC_INTERVAL_MS = 10000
_ROW_HEIGHT = 40


def _wrapped_tooltip(text: str) -> str:
    return "\n".join(textwrap.wrap(text, width=72))


def _mode_keys_for_standing_mode(standing_mode_label: str) -> tuple[str, ...]:
    if standing_mode_label.casefold() == _MODE_LABELS[GAME_MODE_STOCK].casefold():
        return _MODE_KEYS[:-1]
    return _MODE_KEYS


class SteamPanel:
    COLUMNS = ["Game", "Auto UV", "Overlay", "Launch options", ""]
    GAME_COLUMN = 0
    MODE_COLUMN = 1
    OVERLAY_COLUMN = 2
    LAUNCH_OPTIONS_COLUMN = 3
    PLAY_COLUMN = 4

    def __init__(
        self,
        *,
        QtCore,
        QtGui,
        QtWidgets,
        manager: SteamIntegrationManager | None = None,
        adaptive_available=None,
    ):
        self.QtCore = QtCore
        self.QtGui = QtGui
        self.QtWidgets = QtWidgets
        self.manager = manager if manager is not None else SteamIntegrationManager()
        self.adaptive_available = adaptive_available or (lambda: True)
        self._syncing = False
        self._rows: dict[str, SteamGameRow] = {}
        self._restart_thread: threading.Thread | None = None
        self._restart_result: bool | None = None
        self._rescan_thread: threading.Thread | None = None
        self._rescan_rows: tuple[SteamGameRow, ...] | None = None
        self._standing_mode_label = "Stock"
        self._default_mode_label = "Default"

        self.widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(self.widget)
        layout.setContentsMargins(12, 18, 12, 12)
        layout.setSpacing(10)

        header_row = QtWidgets.QHBoxLayout()
        header_row.setSpacing(8)
        self.user_label = QtWidgets.QLabel("")
        self.user_label.setToolTip(
            _wrapped_tooltip(
                "The Steam account currently logged in. Live apply always "
                "targets this account; per-game presets are stored per "
                "account, so switching Steam users never overwrites another "
                "account's presets."
            )
        )
        header_row.addWidget(self.user_label)
        header_row.addStretch(1)
        self.rescan_button = QtWidgets.QPushButton("Rescan library")
        self.rescan_button.setToolTip(
            _wrapped_tooltip(
                "Re-read the installed-game list and each game's launch "
                "options from Steam."
            )
        )
        header_row.addWidget(self.rescan_button)
        self.restart_steam_button = QtWidgets.QPushButton("Restart Steam")
        self.restart_steam_button.setToolTip(
            _wrapped_tooltip(
                "Cleanly shut down the Steam client and relaunch it. Needed "
                "once after initialization, and any time you want Steam "
                "restarted without touching your session."
            )
        )
        header_row.addWidget(self.restart_steam_button)
        layout.addLayout(header_row)

        self.init_banner = QtWidgets.QFrame()
        self.init_banner.setObjectName("steamInitBanner")
        banner_layout = QtWidgets.QHBoxLayout(self.init_banner)
        banner_layout.setContentsMargins(10, 8, 10, 8)
        self.init_label = QtWidgets.QLabel("")
        self.init_label.setWordWrap(True)
        banner_layout.addWidget(self.init_label, 1)
        self.init_button = QtWidgets.QPushButton("Initialize")
        banner_layout.addWidget(self.init_button)
        layout.addWidget(self.init_banner)

        self.table = QtWidgets.QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
        self.table.setIconSize(QtCore.QSize(28, 28))
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(
            self.GAME_COLUMN, QtWidgets.QHeaderView.ResizeToContents
        )
        header.setSectionResizeMode(
            self.LAUNCH_OPTIONS_COLUMN, QtWidgets.QHeaderView.Stretch
        )
        layout.addWidget(self.table, 1)

        self.status_label = QtWidgets.QLabel("")
        layout.addWidget(self.status_label)

        self.rescan_button.clicked.connect(self.rescan)
        self.init_button.clicked.connect(self._initialize)
        self.restart_steam_button.clicked.connect(self._confirm_restart_steam)

        self._sync_timer = QtCore.QTimer(self.widget)
        self._sync_timer.setInterval(_AUTO_SYNC_INTERVAL_MS)
        self._sync_timer.timeout.connect(self._auto_sync)
        self._sync_timer.start()

        self.rescan()

    # -- refresh ------------------------------------------------------------

    def rescan(self) -> None:
        """Full refresh: library, per-account settings, live launch options.

        Reading every game's launch options over CDP takes a couple of
        seconds, so it runs off the UI thread and lands via a poll timer.
        """
        if self._rescan_thread is not None and self._rescan_thread.is_alive():
            return
        self.rescan_button.setEnabled(False)
        # The worker replaces the manager's launch-options cache wholesale;
        # concurrent edits would race it and could be reverted from the stale
        # snapshot, so editing stays off until the scan lands.
        self.table.setEnabled(False)
        self._sync_status("Scanning library…")
        self._rescan_rows = None

        def run() -> None:
            self._rescan_rows = self.manager.refresh(read_launch_options=True)

        self._rescan_thread = threading.Thread(target=run, daemon=True)
        self._rescan_thread.start()
        self._poll_rescan()

    def _poll_rescan(self) -> None:
        thread = self._rescan_thread
        if thread is not None and thread.is_alive():
            self.QtCore.QTimer.singleShot(200, self._poll_rescan)
            return
        self.rescan_button.setEnabled(True)
        if self._rescan_rows is not None:
            self._populate(self._rescan_rows)
        self._sync_header()

    def _auto_sync(self) -> None:
        """Cheap periodic pass: track installs/uninstalls, keep header honest."""
        if self._rescan_thread is not None and self._rescan_thread.is_alive():
            return
        rows = self.manager.refresh(read_launch_options=False)
        if tuple(self._rows) != tuple(row.game.app_id for row in rows):
            self._populate(rows)
        self._sync_header()

    def _populate(self, rows: tuple[SteamGameRow, ...]) -> None:
        self._syncing = True
        try:
            self._rows = {row.game.app_id: row for row in rows}
            self._standing_mode_label = self.manager.standing_mode_label()
            self._default_mode_label = f"Default ({self._standing_mode_label})"
            self.table.setRowCount(len(rows))
            for index, row in enumerate(rows):
                self._populate_row(index, row)
                self.table.setRowHeight(index, _ROW_HEIGHT)
        finally:
            self._syncing = False
        self._sync_status()

    def _populate_row(self, index: int, row: SteamGameRow) -> None:
        QtWidgets = self.QtWidgets
        app_id = row.game.app_id

        game_item = self.QtWidgets.QTableWidgetItem(row.game.name)
        if row.game.icon_path is not None:
            game_item.setIcon(self.QtGui.QIcon(str(row.game.icon_path)))
        runtime = row.game.compat_tool if row.game.is_proton else "native"
        game_item.setToolTip(f"{row.game.name} (app id {app_id}, {runtime})")
        self.table.setItem(index, self.GAME_COLUMN, game_item)

        mode_combo = QtWidgets.QComboBox()
        mode_keys = _mode_keys_for_standing_mode(self._standing_mode_label)
        adaptive_ok = bool(self.adaptive_available())
        for key in mode_keys:
            label = (
                self._default_mode_label if key == GAME_MODE_DEFAULT else _MODE_LABELS[key]
            )
            mode_combo.addItem(label, key)
        if not adaptive_ok:
            model_item = mode_combo.model().item(mode_keys.index(GAME_MODE_ADAPTIVE))
            model_item.setEnabled(False)
            mode_combo.setToolTip(
                _wrapped_tooltip(
                    "Adaptive needs at least two verified Auto-UV tiers; run "
                    "Auto-UV scans first."
                )
            )
        current_mode = normalize_game_mode(row.setting.mode)
        if current_mode not in mode_keys:
            current_mode = GAME_MODE_DEFAULT
        mode_combo.setCurrentIndex(mode_keys.index(current_mode))
        mode_combo.currentIndexChanged.connect(
            lambda _index, app_id=app_id, combo=mode_combo: self._mode_changed(
                app_id, combo
            )
        )
        self.table.setCellWidget(index, self.MODE_COLUMN, mode_combo)

        overlay_checkbox = QtWidgets.QCheckBox()
        overlay_checkbox.setChecked(row.setting.overlay)
        overlay_checkbox.toggled.connect(
            lambda checked, app_id=app_id: self._overlay_changed(app_id, checked)
        )
        overlay_container = QtWidgets.QWidget()
        overlay_layout = QtWidgets.QHBoxLayout(overlay_container)
        overlay_layout.setContentsMargins(0, 0, 0, 0)
        overlay_layout.setAlignment(self.QtCore.Qt.AlignCenter)
        overlay_layout.addWidget(overlay_checkbox)
        self.table.setCellWidget(index, self.OVERLAY_COLUMN, overlay_container)

        launch_edit = QtWidgets.QLineEdit(row.launch_options)
        launch_edit.setToolTip(
            _wrapped_tooltip(
                "The game's Steam launch options, editable. Changes apply "
                "immediately and Steam remembers them for every launch."
            )
        )
        launch_edit.editingFinished.connect(
            lambda app_id=app_id, edit=launch_edit: self._launch_options_edited(
                app_id, edit
            )
        )
        self.table.setCellWidget(index, self.LAUNCH_OPTIONS_COLUMN, launch_edit)

        play_button = QtWidgets.QPushButton("Play")
        play_button.setToolTip(
            _wrapped_tooltip(
                "Launch this game through Steam without switching windows — "
                "the current preset applies exactly as from the Steam client."
            )
        )
        play_button.clicked.connect(
            lambda _checked=False, app_id=app_id: self._play_game(app_id)
        )
        self.table.setCellWidget(index, self.PLAY_COLUMN, play_button)

    # -- header / status ------------------------------------------------------

    def _sync_header(self) -> None:
        user = self.manager.active_user()
        self.user_label.setText(
            f"Steam user: {user.display_name}" if user is not None else "Steam user: —"
        )
        marker = self.manager.marker_present()
        running = self.manager.steam_running()
        cdp_ready = self.manager.cdp_ready() if marker and running else False
        if not marker:
            self.init_label.setText(
                "Live apply is not initialized. Initialization creates Steam's "
                "remote-debugging marker file (a local-only control channel) "
                "and needs one Steam restart."
            )
            self.init_button.setText("Initialize")
            self.init_banner.setVisible(True)
        elif running and not cdp_ready:
            self.init_label.setText(
                "Restart Steam once to activate live apply of launch options."
            )
            self.init_button.setText("Restart Steam now")
            self.init_banner.setVisible(True)
        else:
            self.init_banner.setVisible(False)
        self._live_ready = cdp_ready or not running
        rescan_running = (
            self._rescan_thread is not None and self._rescan_thread.is_alive()
        )
        self.table.setEnabled(self._live_ready and not rescan_running)
        self._sync_status()

    def _sync_status(self, message: str = "") -> None:
        configured = sum(1 for row in self._rows.values() if row.setting.active)
        parts = [f"{len(self._rows)} games", f"{configured} configured"]
        if getattr(self, "_live_ready", False):
            parts.append(
                "live apply" if self.manager.steam_running() else "Steam stopped"
            )
        else:
            parts.append("read-only until initialized")
        if message:
            parts.append(message)
        self.status_label.setText(" · ".join(parts))

    # -- edit handlers ---------------------------------------------------------

    def _mode_changed(self, app_id: str, combo) -> None:
        if self._syncing:
            return
        mode = combo.currentData()
        result = self.manager.set_game_mode(app_id, mode)
        hot = self.manager.hot_reapply(app_id) if result.ok else None
        self._after_apply(app_id, result, extra=hot)

    def _overlay_changed(self, app_id: str, checked: bool) -> None:
        if self._syncing:
            return
        result = self.manager.set_game_overlay(app_id, checked)
        self._after_apply(app_id, result)

    def _launch_options_edited(self, app_id: str, edit) -> None:
        if self._syncing:
            return
        row = self._rows.get(app_id)
        text = edit.text().strip()
        if row is not None and text == row.launch_options:
            return
        result = self.manager.set_raw_launch_options(app_id, text)
        self._after_apply(app_id, result)

    def _play_game(self, app_id: str) -> None:
        row = self._rows.get(app_id)
        name = row.game.name if row is not None else app_id
        if launch_steam_game(app_id):
            self._sync_status(f"{name}: launching via Steam…")
        else:
            self._sync_status(f"{name}: FAILED to launch (steam not available)")

    def _after_apply(self, app_id: str, result, extra=None) -> None:
        rows = self.manager.refresh(read_launch_options=False)
        self._populate(rows)
        row = self._rows.get(app_id)
        name = row.game.name if row is not None else app_id
        prefix = "" if result.ok else "FAILED: "
        message = f"{name}: {prefix}{result.message}"
        if extra is not None:
            message += f" {extra.message}"
        self._sync_status(message)

    # -- init / restart ---------------------------------------------------------

    def _initialize(self) -> None:
        if self.init_button.text() == "Restart Steam now":
            self._confirm_restart_steam()
            return
        result = self.manager.initialize()
        self._sync_header()
        self._sync_status(result.message)

    def _confirm_restart_steam(self) -> None:
        QtWidgets = self.QtWidgets
        if self._restart_thread is not None and self._restart_thread.is_alive():
            return
        answer = QtWidgets.QMessageBox.question(
            self.widget,
            "Restart Steam",
            "Cleanly shut down and relaunch the Steam client now?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return
        self.restart_steam_button.setEnabled(False)
        self._sync_status("Restarting Steam…")
        self._restart_result = None

        def run() -> None:
            self._restart_result = restart_steam()

        self._restart_thread = threading.Thread(target=run, daemon=True)
        self._restart_thread.start()
        self._poll_restart()

    def _poll_restart(self) -> None:
        thread = self._restart_thread
        if thread is not None and thread.is_alive():
            self.QtCore.QTimer.singleShot(500, self._poll_restart)
            return
        self.restart_steam_button.setEnabled(True)
        if self._restart_result:
            self._sync_status("Steam restarted.")
        else:
            self._sync_status("Steam restart failed.")
        self._sync_header()
