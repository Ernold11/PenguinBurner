"""Steam tab: a library list and one focused per-game launcher.

The left pane is only for choosing an installed game. The right pane owns the
single editable Steam command line, Auto-UV mode, overlay toggle, and Play
button for that selection. Changes still apply immediately through
``integrations.steam.manager`` and persist per Steam account.
"""

from __future__ import annotations

from datetime import datetime
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

SORT_RECENT = "recent"
SORT_ALPHABETICAL = "alphabetical"

_AUTO_SYNC_INTERVAL_MS = 10000
_ROW_HEIGHT = 42


def _wrapped_tooltip(text: str) -> str:
    return "\n".join(textwrap.wrap(text, width=72))


def _mode_keys_for_standing_mode(standing_mode_label: str) -> tuple[str, ...]:
    if standing_mode_label.casefold() == _MODE_LABELS[GAME_MODE_STOCK].casefold():
        return _MODE_KEYS[:-1]
    return _MODE_KEYS


def sorted_steam_rows(
    rows: tuple[SteamGameRow, ...], sort_mode: str
) -> tuple[SteamGameRow, ...]:
    """Sort the visible library; never-played games trail recent games."""
    if sort_mode == SORT_ALPHABETICAL:
        return tuple(
            sorted(rows, key=lambda row: (row.game.name.casefold(), row.game.app_id))
        )
    return tuple(
        sorted(
            rows,
            key=lambda row: (
                0 if row.game.last_played > 0 else 1,
                -row.game.last_played,
                row.game.name.casefold(),
                row.game.app_id,
            ),
        )
    )


def last_played_text(timestamp: int) -> str:
    if timestamp <= 0:
        return "Never played on this PC"
    try:
        played = datetime.fromtimestamp(timestamp).astimezone()
    except (OSError, OverflowError, ValueError):
        return "Last played: unknown"
    return f"Last played: {played:%Y-%m-%d %H:%M}"


class SteamPanel:
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
        self._scan_running = False
        self._rows: dict[str, SteamGameRow] = {}
        self._selected_app_id = ""
        self._restart_thread: threading.Thread | None = None
        self._restart_result: bool | None = None
        self._rescan_thread: threading.Thread | None = None
        self._rescan_rows: tuple[SteamGameRow, ...] | None = None
        self._standing_mode_label = "Stock"
        self._default_mode_label = "Default"
        self._live_ready = False

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
                "Re-read the installed-game list, last-played timestamps, and "
                "each game's launch options from Steam."
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

        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.splitter.setObjectName("steamLibrarySplitter")
        self.splitter.setChildrenCollapsible(False)

        self.library_pane = QtWidgets.QFrame()
        self.library_pane.setObjectName("steamLibraryPane")
        self.library_pane.setMinimumWidth(230)
        library_layout = QtWidgets.QVBoxLayout(self.library_pane)
        library_layout.setContentsMargins(10, 10, 10, 10)
        library_layout.setSpacing(8)

        library_title = QtWidgets.QLabel("Installed games")
        library_title.setObjectName("steamPaneTitle")
        library_layout.addWidget(library_title)

        sort_row = QtWidgets.QHBoxLayout()
        sort_row.setSpacing(6)
        sort_row.addWidget(QtWidgets.QLabel("Sort"))
        self.sort_combo = QtWidgets.QComboBox()
        self.sort_combo.setObjectName("steamGameSort")
        self.sort_combo.addItem("Recently played", SORT_RECENT)
        self.sort_combo.addItem("Alphabetical", SORT_ALPHABETICAL)
        self.sort_combo.setToolTip(
            _wrapped_tooltip(
                "Recently played uses Steam's LastPlayed timestamp from each "
                "installed app manifest. Games without a timestamp appear last."
            )
        )
        sort_row.addWidget(self.sort_combo, 1)
        library_layout.addLayout(sort_row)

        self.game_list = QtWidgets.QListWidget()
        self.game_list.setObjectName("steamGameList")
        self.game_list.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.game_list.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
        self.game_list.setIconSize(QtCore.QSize(28, 28))
        self.game_list.setAlternatingRowColors(True)
        library_layout.addWidget(self.game_list, 1)
        self.splitter.addWidget(self.library_pane)
        self.splitter.setCollapsible(0, False)

        self.details_pane = QtWidgets.QFrame()
        self.details_pane.setObjectName("steamGameDetailsPane")
        details_layout = QtWidgets.QVBoxLayout(self.details_pane)
        details_layout.setContentsMargins(22, 18, 22, 18)
        details_layout.setSpacing(12)

        self.game_title = QtWidgets.QLabel("Select a game")
        self.game_title.setObjectName("steamGameTitle")
        self.game_title.setWordWrap(True)
        details_layout.addWidget(self.game_title)

        self.game_metadata = QtWidgets.QLabel("")
        self.game_metadata.setObjectName("steamGameMetadata")
        self.game_metadata.setWordWrap(True)
        details_layout.addWidget(self.game_metadata)

        command_label = QtWidgets.QLabel("Steam command line")
        command_label.setObjectName("steamFieldLabel")
        details_layout.addWidget(command_label)
        self.launch_edit = QtWidgets.QLineEdit()
        self.launch_edit.setObjectName("steamLaunchOptions")
        self.launch_edit.setPlaceholderText("%command%")
        self.launch_edit.setToolTip(
            _wrapped_tooltip(
                "The selected game's one Steam launch-options command line. "
                "Changes apply immediately and Steam remembers them for every launch."
            )
        )
        details_layout.addWidget(self.launch_edit)

        mode_label = QtWidgets.QLabel("Auto-UV mode")
        mode_label.setObjectName("steamFieldLabel")
        details_layout.addWidget(mode_label)
        self.mode_combo = QtWidgets.QComboBox()
        self.mode_combo.setObjectName("steamAutoUvMode")
        for key in _MODE_KEYS:
            self.mode_combo.addItem(_MODE_LABELS[key], key)
        details_layout.addWidget(self.mode_combo)

        self.overlay_checkbox = QtWidgets.QCheckBox("Enable ingame overlay")
        self.overlay_checkbox.setObjectName("steamOverlayToggle")
        details_layout.addWidget(self.overlay_checkbox)

        details_layout.addStretch(1)
        self.play_button = QtWidgets.QPushButton("Play")
        self.play_button.setObjectName("steamPlayButton")
        self.play_button.setMinimumHeight(44)
        self.play_button.setToolTip(
            _wrapped_tooltip(
                "Launch the selected game through Steam. The command line, "
                "Auto-UV mode, and overlay choice above are used for the launch."
            )
        )
        details_layout.addWidget(self.play_button)
        self.splitter.addWidget(self.details_pane)
        self.splitter.setCollapsible(1, False)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([300, 760])
        layout.addWidget(self.splitter, 1)

        self.status_label = QtWidgets.QLabel("")
        layout.addWidget(self.status_label)

        self.rescan_button.clicked.connect(self.rescan)
        self.init_button.clicked.connect(self._initialize)
        self.restart_steam_button.clicked.connect(self._confirm_restart_steam)
        self.sort_combo.currentIndexChanged.connect(self._sort_changed)
        self.game_list.currentItemChanged.connect(self._selection_changed)
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        self.overlay_checkbox.toggled.connect(self._overlay_changed)
        self.launch_edit.editingFinished.connect(self._launch_options_edited)
        self.play_button.clicked.connect(self._play_game)

        self._sync_timer = QtCore.QTimer(self.widget)
        self._sync_timer.setInterval(_AUTO_SYNC_INTERVAL_MS)
        self._sync_timer.timeout.connect(self._auto_sync)
        self._sync_timer.start()

        self._sync_selected_details()
        self.rescan()

    # -- refresh ------------------------------------------------------------

    def rescan(self) -> None:
        """Refresh the library and Steam command lines outside the UI thread."""
        if self._rescan_thread is not None and self._rescan_thread.is_alive():
            return
        self._scan_running = True
        self.rescan_button.setEnabled(False)
        self._sync_interaction_state()
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
        self._scan_running = False
        self.rescan_button.setEnabled(True)
        if self._rescan_rows is not None:
            self._populate(self._rescan_rows)
        self._sync_header()

    def _auto_sync(self) -> None:
        """Track installs and updated LastPlayed values without a full CDP read."""
        if self._rescan_thread is not None and self._rescan_thread.is_alive():
            return
        rows = self.manager.refresh(read_launch_options=False)
        if self._row_signature(rows) != self._row_signature(tuple(self._rows.values())):
            self._populate(rows)
        else:
            self._sync_default_mode_label()
        self._sync_header()

    @staticmethod
    def _row_signature(rows: tuple[SteamGameRow, ...]) -> tuple[tuple[object, ...], ...]:
        return tuple(
            (
                row.game.app_id,
                row.game.name,
                row.game.last_played,
                row.game.icon_path,
                row.game.compat_tool,
                row.setting,
                row.launch_options,
            )
            for row in rows
        )

    def _populate(self, rows: tuple[SteamGameRow, ...]) -> None:
        previous_app_id = self._selected_app_id or self._current_app_id()
        self._rows = {row.game.app_id: row for row in rows}
        self._sync_default_mode_label()
        self._refresh_game_list(preferred_app_id=previous_app_id)
        self._sync_status()

    def _sync_default_mode_label(self) -> None:
        self._standing_mode_label = self.manager.standing_mode_label()
        self._default_mode_label = f"Default ({self._standing_mode_label})"
        mode_keys = _mode_keys_for_standing_mode(self._standing_mode_label)
        current_mode = normalize_game_mode(self.mode_combo.currentData())
        if current_mode not in mode_keys:
            current_mode = GAME_MODE_DEFAULT
        signals_were_blocked = self.mode_combo.blockSignals(True)
        try:
            self.mode_combo.clear()
            for key in mode_keys:
                label = (
                    self._default_mode_label
                    if key == GAME_MODE_DEFAULT
                    else _MODE_LABELS[key]
                )
                self.mode_combo.addItem(label, key)
            self.mode_combo.setCurrentIndex(mode_keys.index(current_mode))
        finally:
            self.mode_combo.blockSignals(signals_were_blocked)

        adaptive_ok = bool(self.adaptive_available())
        adaptive_index = self.mode_combo.findData(GAME_MODE_ADAPTIVE)
        if adaptive_index >= 0:
            item = self.mode_combo.model().item(adaptive_index)
            if item is not None:
                item.setEnabled(adaptive_ok)
        if not adaptive_ok:
            self.mode_combo.setToolTip(
                _wrapped_tooltip(
                    "Adaptive needs at least two verified Auto-UV tiers; run "
                    "Auto-UV scans first."
                )
            )
        else:
            self.mode_combo.setToolTip("")

    def _refresh_game_list(self, *, preferred_app_id: str = "") -> None:
        sort_mode = str(self.sort_combo.currentData() or SORT_RECENT)
        rows = sorted_steam_rows(tuple(self._rows.values()), sort_mode)
        selected_item = None
        self._syncing = True
        try:
            self.game_list.clear()
            for row in rows:
                item = self.QtWidgets.QListWidgetItem(row.game.name)
                item.setData(self.QtCore.Qt.UserRole, row.game.app_id)
                item.setSizeHint(self.QtCore.QSize(0, _ROW_HEIGHT))
                if row.game.icon_path is not None:
                    item.setIcon(self.QtGui.QIcon(str(row.game.icon_path)))
                runtime = row.game.compat_tool if row.game.is_proton else "Native Linux"
                item.setToolTip(
                    f"{row.game.name}\nApp {row.game.app_id} · {runtime}\n"
                    f"{last_played_text(row.game.last_played)}"
                )
                self.game_list.addItem(item)
                if row.game.app_id == preferred_app_id:
                    selected_item = item
            if selected_item is None and self.game_list.count():
                selected_item = self.game_list.item(0)
            self.game_list.setCurrentItem(selected_item)
        finally:
            self._syncing = False
        self._sync_selected_details()

    # -- selected game ------------------------------------------------------

    def _current_app_id(self) -> str:
        item = self.game_list.currentItem()
        return str(item.data(self.QtCore.Qt.UserRole) or "") if item is not None else ""

    def _selection_changed(self, _current, _previous) -> None:
        if not self._syncing:
            self._sync_selected_details()

    def _sort_changed(self, _index: int) -> None:
        if self._syncing:
            return
        self._refresh_game_list(preferred_app_id=self._current_app_id())

    def _sync_selected_details(self) -> None:
        app_id = self._current_app_id()
        row = self._rows.get(app_id)
        self._selected_app_id = app_id if row is not None else ""
        was_syncing = self._syncing
        self._syncing = True
        try:
            if row is None:
                self.game_title.setText("Select a game")
                self.game_metadata.setText("")
                self.launch_edit.clear()
                self.mode_combo.setCurrentIndex(
                    max(0, self.mode_combo.findData(GAME_MODE_DEFAULT))
                )
                self.overlay_checkbox.setChecked(False)
            else:
                self.game_title.setText(row.game.name)
                runtime = row.game.compat_tool if row.game.is_proton else "Native Linux"
                self.game_metadata.setText(
                    f"App {row.game.app_id} · {runtime} · "
                    f"{last_played_text(row.game.last_played)}"
                )
                self.launch_edit.setText(row.launch_options)
                mode_index = self.mode_combo.findData(normalize_game_mode(row.setting.mode))
                self.mode_combo.setCurrentIndex(max(0, mode_index))
                self.overlay_checkbox.setChecked(row.setting.overlay)
        finally:
            self._syncing = was_syncing
        self._sync_interaction_state()

    def _sync_interaction_state(self) -> None:
        has_selection = bool(self._selected_app_id and self._selected_app_id in self._rows)
        self.game_list.setEnabled(not self._scan_running)
        self.sort_combo.setEnabled(not self._scan_running)
        editable = has_selection and self._live_ready and not self._scan_running
        self.launch_edit.setEnabled(has_selection)
        self.launch_edit.setReadOnly(not editable)
        self.mode_combo.setEnabled(editable)
        self.overlay_checkbox.setEnabled(editable)
        # Playing does not mutate Steam configuration, so it remains available
        # even before live launch-option apply has been initialized.
        self.play_button.setEnabled(has_selection)

    # -- header / status ----------------------------------------------------

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
        self._sync_interaction_state()
        self._sync_status()

    def _sync_status(self, message: str = "") -> None:
        configured = sum(1 for row in self._rows.values() if row.setting.active)
        parts = [f"{len(self._rows)} games", f"{configured} configured"]
        if self._live_ready:
            parts.append("live apply" if self.manager.steam_running() else "Steam stopped")
        else:
            parts.append("read-only until initialized")
        if message:
            parts.append(message)
        self.status_label.setText(" · ".join(parts))

    # -- edit handlers -----------------------------------------------------

    def _mode_changed(self, _index: int) -> None:
        if self._syncing:
            return
        app_id = self._current_app_id()
        if not app_id:
            return
        result = self.manager.set_game_mode(app_id, self.mode_combo.currentData())
        hot = self.manager.hot_reapply(app_id) if result.ok else None
        self._after_apply(app_id, result, extra=hot)

    def _overlay_changed(self, checked: bool) -> None:
        if self._syncing:
            return
        app_id = self._current_app_id()
        if not app_id:
            return
        result = self.manager.set_game_overlay(app_id, checked)
        self._after_apply(app_id, result)

    def _launch_options_edited(self) -> None:
        if self._syncing:
            return
        app_id = self._current_app_id()
        row = self._rows.get(app_id)
        text = self.launch_edit.text().strip()
        if row is None or text == row.launch_options:
            return
        result = self.manager.set_raw_launch_options(app_id, text)
        self._after_apply(app_id, result)

    def _play_game(self, _checked: bool = False) -> None:
        app_id = self._current_app_id()
        row = self._rows.get(app_id)
        if row is None:
            return
        if launch_steam_game(app_id):
            self._sync_status(f"{row.game.name}: launching via Steam…")
        else:
            self._sync_status(
                f"{row.game.name}: FAILED to launch (steam not available)"
            )

    def _after_apply(self, app_id: str, result, extra=None) -> None:
        rows = self.manager.refresh(read_launch_options=False)
        self._selected_app_id = app_id
        self._populate(rows)
        row = self._rows.get(app_id)
        name = row.game.name if row is not None else app_id
        prefix = "" if result.ok else "FAILED: "
        message = f"{name}: {prefix}{result.message}"
        if extra is not None:
            message += f" {extra.message}"
        self._sync_status(message)

    # -- init / restart ----------------------------------------------------

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
