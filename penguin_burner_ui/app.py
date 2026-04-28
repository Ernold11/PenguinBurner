from __future__ import annotations

import html
import json
import math
import os
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from datetime import datetime
from importlib.resources import files
from pathlib import Path
import re
import signal
import shlex
import subprocess
import sys

from afterburner.fan_curve import load_afterburner_fan_settings
from afterburner.import_fan_curve import load_config, write_config
from afterburner.import_vf_curve import (
    load_afterburner_runtime_options,
    persist_afterburner_import,
)
from afterburner.vfcurve import (
    describe_afterburner_flatten_validation,
    discover_afterburner_vf_sections,
    resolve_afterburner_vf_source,
)
from auto_uv.manual_curve_editor import (
    ManualCurveEdit,
    editable_anchor_from_profile,
    manual_drag_anchor_edit,
    manual_flatten_from_existing_point,
    manual_nudge_selected_voltage,
    manual_nudge_selected_frequency,
    manual_offset_selected_range,
    manual_select_adjacent_point,
    manual_select_curve_point,
    manual_select_range_to_right,
    manual_tune_single_point_edit,
    user_edited_profile_payload,
)
from auto_uv.manual_fan_curve_editor import user_edited_fan_curve_profile_payload
from auto_uv.profiles import (
    archive_auto_uv_profile,
    delete_auto_uv_profile_paths,
    profile_display_name,
    read_auto_uv_profile_summaries,
)
from auto_uv.sweep_modes import AUTO_UV_MODE_EFFICIENCY, AUTO_UV_MODE_PERFORMANCE
from auto_uv.tuning import (
    AUTO_UV_MAX_CLOCK_BUMP_BUDGET_RATIO,
    AUTO_UV_YOLO_MAX_CLOCK_BUMP_BUDGET_RATIO,
)
from auto_uv.user_output import format_user_duration as _format_duration_for_user
from lact import (
    LactExportError,
    write_lact_nvidia_config,
    write_lact_nvidia_config_from_afterburner,
)
from penguin_burner_paths import (
    claim_desktop_user_ownership,
    default_runtime_config_path,
    default_user_config_dir,
    discover_afterburner_device_profiles,
    managed_afterburner_root,
    resolve_afterburner_root,
    sync_afterburner_export_tree,
)
from runtime_service import (
    PENGUIN_BURNER_UNIT_NAME,
    SYSTEMCTL,
    systemd_service_unit_path,
)

from .commands import (
    delete_profiles_command,
    profile_reverify_command,
    runtime_profile_command,
    scan_command,
)
from .components import (
    CurvePlot,
    LogView,
    ProfileList,
    RunsTable,
    ScanControls,
    StatusHeader,
)
from .components.fan_curve_editor import (
    fan_curve_editor_shortcut_legend_rows,
    open_fan_curve_editor_dialog,
)
from .components.table_sizing import set_header_fit_column_widths


DEFAULT_FINAL_VERIFICATION_DURATION_S = 600
MAX_FINAL_VERIFICATION_DURATION_S = 3600
DEFAULT_SHORT_VERIFICATION_BASE_S = 30
PERFORMANCE_BIAS_WARNING_PCT = 110.0
PERFORMANCE_BIAS_DANGER_PCT = 130.0
MAX_OVERCLOCK_BUDGET_PCT = AUTO_UV_MAX_CLOCK_BUMP_BUDGET_RATIO * 100.0
YOLO_MAX_OVERCLOCK_BUDGET_PCT = AUTO_UV_YOLO_MAX_CLOCK_BUMP_BUDGET_RATIO * 100.0
DEFAULT_AUTO_UV_PERFORMANCE_BIAS_PCT = 100.0
DEFAULT_AUTO_UV_MAX_DROP_PCT = 15.0
DEFAULT_AUTO_UV_MAX_CLOCK_DROP_PCT = 10.0
FINAL_CHOICE_SORT_ROLE = 261
FINAL_CHOICE_FPSW_SORT_COLUMN = 3
FINAL_CHOICE_FPS_SORT_COLUMN = 4
FINAL_CHOICE_DEFAULT_SORT_COLUMN = FINAL_CHOICE_FPSW_SORT_COLUMN
FINAL_CHOICE_SORTABLE_COLUMNS = frozenset({2, 3, 4, 5})
FINAL_CHOICE_HIGHER_FIRST_COLUMNS = frozenset({2, 3, 4})
LACT_CONFIG_FILENAME = "config.yaml"
AFTERBURNER_PROFILE_ID = "afterburner-import"
APP_DESKTOP_ID = "io.github.jpietek.PenguinBurner"
APP_DISPLAY_NAME = "Nvidia GPU Undervolting Tool"
APP_ICON_NAME = "penguin-burner"
GPU_UNDERVOLTING_PURPOSE_TEXT = (
    "GPU undervolting is meant to make your graphics card consume significantly "
    "less power while giving up as little performance as possible. The practical "
    "result can be dead-silent fan operation, lower temperatures, and lower "
    "electricity bills. PenguinBurner automatically searches for the operating "
    "sweet spot of your Nvidia GPU, so you do not have to resort to trial and "
    "error or risk introducing avoidable system instability."
)
PERFORMANCE_BIAS_TOOLTIP_TEXT = (
    "Controls how strongly Auto-UV favors recovering clocks after lowering "
    "voltage. Moving toward Performance allows more aggressive clock recovery. "
    "The far Performance side can push the curve above the measured baseline "
    "clock and might hang your system. Changing this can result in instability; "
    "modify with care."
)


def _curve_editor_shortcut_legend_rows() -> tuple[tuple[str, str], ...]:
    return (
        ("Click", "select dot"),
        ("Ctrl+Click", "new point"),
        ("Drag", "move selection"),
        ("Up/Down", "clock"),
        ("Left/Right", "voltage"),
        ("Tab / Shift+Tab", "next / previous"),
        ("Shift+Right", "range right"),
        ("Ctrl+L", "lock here"),
        ("Ctrl+Z / Ctrl+Y", "undo / redo"),
    )


def _fan_curve_editor_shortcut_legend_rows() -> tuple[tuple[str, str], ...]:
    return fan_curve_editor_shortcut_legend_rows()


def _auto_uv_mode_for_performance_bias(value_pct: float | int) -> str:
    if float(value_pct) >= 100.0:
        return AUTO_UV_MODE_PERFORMANCE
    return AUTO_UV_MODE_EFFICIENCY


def _performance_bias_clock_recovery_pct(
    slider_position: float | int,
    *,
    max_pct: float | int = MAX_OVERCLOCK_BUDGET_PCT,
) -> float:
    clamped = max(0.0, min(100.0, float(slider_position)))
    if clamped <= 50.0:
        return clamped * 2.0
    right_span = max(0.0, float(max_pct) - 100.0)
    return 100.0 + ((clamped - 50.0) / 50.0 * right_span)


def _performance_bias_slider_position(
    value_pct: float | int,
    *,
    max_pct: float | int = MAX_OVERCLOCK_BUDGET_PCT,
) -> int:
    clamped = max(0.0, min(float(max_pct), float(value_pct)))
    if clamped <= 100.0:
        return int(round(clamped / 2.0))
    right_span = max(1.0, float(max_pct) - 100.0)
    return int(round(50.0 + ((clamped - 100.0) / right_span * 50.0)))


def _slider_value_from_click_position(
    *,
    position_px: float | int,
    width_px: float | int,
    minimum: int,
    maximum: int,
    inverted: bool = False,
) -> int:
    span = max(1.0, float(width_px) - 1.0)
    ratio = max(0.0, min(1.0, float(position_px) / span))
    if bool(inverted):
        ratio = 1.0 - ratio
    return int(round(float(minimum) + ratio * float(int(maximum) - int(minimum))))


def _click_jump_slider_class(QtCore, QtWidgets):
    class ClickJumpSlider(QtWidgets.QSlider):
        def _event_x(self, event) -> float:
            position = event.position() if hasattr(event, "position") else event.pos()
            return float(position.x())

        def _set_value_from_event(self, event) -> None:
            self.setValue(
                _slider_value_from_click_position(
                    position_px=self._event_x(event),
                    width_px=self.width(),
                    minimum=self.minimum(),
                    maximum=self.maximum(),
                    inverted=self.invertedAppearance(),
                )
            )

        def mousePressEvent(self, event) -> None:
            left_button = getattr(QtCore.Qt.MouseButton, "LeftButton", QtCore.Qt.LeftButton)
            if event.button() == left_button:
                self.setSliderDown(True)
                self._set_value_from_event(event)
                event.accept()
                return
            super().mousePressEvent(event)

        def mouseMoveEvent(self, event) -> None:
            left_button = getattr(QtCore.Qt.MouseButton, "LeftButton", QtCore.Qt.LeftButton)
            if event.buttons() & left_button:
                self._set_value_from_event(event)
                event.accept()
                return
            super().mouseMoveEvent(event)

        def mouseReleaseEvent(self, event) -> None:
            left_button = getattr(QtCore.Qt.MouseButton, "LeftButton", QtCore.Qt.LeftButton)
            if event.button() == left_button:
                self._set_value_from_event(event)
                self.setSliderDown(False)
                event.accept()
                return
            super().mouseReleaseEvent(event)

    return ClickJumpSlider


def _import_qt():
    try:
        from PySide6 import QtCore, QtGui, QtWidgets
    except ImportError as exc:
        raise RuntimeError(
            "PySide6 is required for the UI. Install it with "
            "`python -m pip install penguin-burner`."
        ) from exc
    try:
        import pyqtgraph as pg
    except ImportError:
        pg = None
    return QtCore, QtGui, QtWidgets, pg


class AutoUvWindow:
    def __init__(self, qt_modules, *, yolo: bool = False):
        self.QtCore, self.QtGui, self.QtWidgets, self.pg = qt_modules
        self.auto_uv_yolo = bool(yolo)
        self.process = None
        self.runtime_process = None
        self.runtime_process_kind = ""
        self.reverify_target_duration_s = 0
        self.reverify_last_elapsed_s = 0.0
        self.reverify_stop_requested = False
        self.active_reverify_stop_request_path: Path | None = None
        self.profile_delete_process = None
        self._profile_delete_selected_ids = set()
        self._profile_delete_remove_systemd = False
        self.active_stop_request_path: Path | None = None
        self.last_auto_uv_candidate_id = ""
        self.last_auto_uv_profile_id = ""
        self.pending_final_result_payload: dict | None = None
        self.final_choice_discarded = False
        self.profile_summaries: list[dict] = []
        self.fixed_tab_widgets: list[object] = []
        self.profile_curve_widgets: dict[str, object] = {}
        self.base_curve_points: list[tuple[float, float]] = (
            _load_cached_base_curve_points()
        )
        self.profile_disabled_for_scan = False
        self.fan_measured_points: list[tuple[float, float]] = []
        self.stop_requested = False
        self.window = self.QtWidgets.QMainWindow()
        self.window.setWindowTitle(APP_DISPLAY_NAME)
        icon = _application_icon(self.QtGui)
        if not icon.isNull():
            self.window.setWindowIcon(icon)
        self.window.resize(1220, 820)
        self._build_ui()

    def _build_ui(self) -> None:
        root = self.QtWidgets.QWidget()
        layout = self.QtWidgets.QVBoxLayout(root)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        self.header = StatusHeader(QtCore=self.QtCore, QtWidgets=self.QtWidgets)
        self.controls = ScanControls(
            QtWidgets=self.QtWidgets,
        )
        self.vf_plot = CurvePlot(
            QtWidgets=self.QtWidgets,
            pg=self.pg,
            x_label="Voltage",
            x_units="mV",
            y_label="Clock",
            y_units="MHz",
            x_range=(800, 1100),
            y_range=(1000, 3000),
        )
        self.fan_plot = CurvePlot(
            QtWidgets=self.QtWidgets,
            pg=self.pg,
            x_label="Temperature",
            x_units="C",
            y_label="Fan",
            y_units="%",
            x_range=(35, 95),
            y_range=(0, 100),
        )
        self.runs_table = RunsTable(
            QtCore=self.QtCore,
            QtGui=self.QtGui,
            QtWidgets=self.QtWidgets,
        )
        self.runs_table.on_candidate_selection_changed = (
            self._handle_runs_table_candidate_selection
        )
        self.profile_list = ProfileList(
            QtCore=self.QtCore,
            QtGui=self.QtGui,
            QtWidgets=self.QtWidgets,
        )
        self.log_view = LogView(
            QtCore=self.QtCore,
            QtGui=self.QtGui,
            QtWidgets=self.QtWidgets,
        )

        auto_uv_view = self.QtWidgets.QSplitter(self.QtCore.Qt.Horizontal)
        auto_uv_view.addWidget(self.vf_plot.widget)
        auto_uv_view.addWidget(self.log_view.widget)
        auto_uv_view.setSizes([760, 440])

        self.tabs = self.QtWidgets.QTabWidget()
        self.auto_uv_tab_index = self.tabs.addTab(auto_uv_view, "Auto-UV")
        self.tabs.addTab(self.fan_plot.widget, "Silent Fan Curve")
        self.profiles_tab_index = self.tabs.addTab(self.profile_list.widget, "Profiles")
        self.fixed_tab_widgets = [
            auto_uv_view,
            self.fan_plot.widget,
            self.profile_list.widget,
        ]
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self._close_curve_tab)
        self._sync_tab_close_buttons()
        table_panel = self.QtWidgets.QGroupBox("Undervolting runs")
        table_panel.setMinimumHeight(220)
        table_layout = self.QtWidgets.QVBoxLayout(table_panel)
        table_layout.setContentsMargins(10, 18, 10, 10)
        table_layout.addWidget(self.runs_table.widget)

        layout.addWidget(self.header.widget)
        layout.addWidget(self.controls.widget)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(table_panel)

        self.controls.start_button.clicked.connect(self.start_scan)
        self.controls.stop_button.clicked.connect(self.stop_scan)
        self.controls.import_afterburner_button.clicked.connect(
            self._import_afterburner_profile
        )
        self.controls.about_button.clicked.connect(self._show_about_dialog)
        self.tabs.currentChanged.connect(self._tab_changed)
        self.profile_list.daemonize_button.clicked.connect(self._run_selected_profile)
        self.profile_list.delete_button.clicked.connect(self._delete_selected_profiles)
        self.profile_list.install_button.toggled.connect(
            lambda _checked: self._update_runner_status()
        )
        self.profile_list.remove_button.clicked.connect(
            lambda: self._run_runtime_action("uninstall-systemd")
        )
        self.profile_list.table.itemSelectionChanged.connect(self._update_runner_status)
        self.profile_list.table.doubleClicked.connect(self._view_profile_curve_from_index)
        context_menu_policy = getattr(
            getattr(self.QtCore.Qt, "ContextMenuPolicy", self.QtCore.Qt),
            "CustomContextMenu",
        )
        self.profile_list.table.setContextMenuPolicy(context_menu_policy)
        self.profile_list.table.customContextMenuRequested.connect(
            self._show_profile_context_menu
        )
        self.window.setCentralWidget(root)
        self.window.setStyleSheet(STYLESHEET)
        self.profile_refresh_timer = self.QtCore.QTimer(self.window)
        self.profile_refresh_timer.setInterval(3000)
        self.profile_refresh_timer.timeout.connect(self._refresh_profiles_if_visible)
        self.profile_refresh_timer.start()
        self._load_profiles()
        self._refresh_saved_fan_curve_plot()

    def show(self) -> None:
        self.window.show()

    def _show_about_dialog(self) -> None:
        dialog = self.QtWidgets.QDialog(self.window)
        dialog.setWindowTitle("About")
        dialog.setMinimumWidth(520)
        layout = self.QtWidgets.QVBoxLayout(dialog)
        layout.setContentsMargins(24, 24, 24, 18)
        layout.setSpacing(12)

        logo_label = self.QtWidgets.QLabel()
        logo_label.setAlignment(self.QtCore.Qt.AlignCenter)
        icon_path = _application_icon_path()
        pixmap = self.QtGui.QPixmap(str(icon_path)) if icon_path is not None else None
        if pixmap is not None and not pixmap.isNull():
            aspect_mode = getattr(
                getattr(self.QtCore.Qt, "AspectRatioMode", self.QtCore.Qt),
                "KeepAspectRatio",
            )
            transform_mode = getattr(
                getattr(self.QtCore.Qt, "TransformationMode", self.QtCore.Qt),
                "SmoothTransformation",
            )
            logo_label.setPixmap(pixmap.scaled(180, 180, aspect_mode, transform_mode))
        layout.addWidget(logo_label)

        title = self.QtWidgets.QLabel(APP_DISPLAY_NAME)
        title.setAlignment(self.QtCore.Qt.AlignCenter)
        title.setObjectName("aboutTitle")
        title.setTextInteractionFlags(_selectable_text_flags(self.QtCore))
        layout.addWidget(title)

        version_label = self.QtWidgets.QLabel(f"Version {_application_version()}")
        version_label.setAlignment(self.QtCore.Qt.AlignCenter)
        version_label.setObjectName("aboutVersion")
        version_label.setTextInteractionFlags(_selectable_text_flags(self.QtCore))
        layout.addWidget(version_label)

        purpose = self.QtWidgets.QLabel(GPU_UNDERVOLTING_PURPOSE_TEXT)
        purpose.setObjectName("purposeText")
        purpose.setWordWrap(True)
        purpose.setAlignment(self.QtCore.Qt.AlignCenter)
        purpose.setTextInteractionFlags(_selectable_text_flags(self.QtCore))
        layout.addWidget(purpose)

        body = self.QtWidgets.QLabel(
            "If you like the tool please consider supporting me on Github!<br>"
            '<a href="https://github.com/sponsors/jpietek">'
            "https://github.com/sponsors/jpietek</a><br><br>"
            "Having issues with PenguinBurner? Please report the bugs here:<br>"
            '<a href="https://github.com/jpietek/PenguinBurner/issues">'
            "https://github.com/jpietek/PenguinBurner/issues</a>"
        )
        body.setAlignment(self.QtCore.Qt.AlignCenter)
        body.setWordWrap(True)
        body.setOpenExternalLinks(True)
        body.setTextInteractionFlags(
            _qt_flags(
                self.QtCore.Qt,
                "TextInteractionFlag",
                "TextBrowserInteraction",
                "TextSelectableByMouse",
                "TextSelectableByKeyboard",
                "LinksAccessibleByKeyboard",
            )
        )
        layout.addWidget(body)

        buttons = self.QtWidgets.QDialogButtonBox(
            self.QtWidgets.QDialogButtonBox.StandardButton.Ok
        )
        buttons.accepted.connect(dialog.accept)
        layout.addWidget(buttons)
        dialog.exec()

    def start_scan(self) -> None:
        if (
            self.process is not None
            or self.runtime_process is not None
            or self.profile_delete_process is not None
        ):
            return
        tuning_options = self._select_auto_uv_tuning_dialog()
        if tuning_options is None:
            return

        stop_request_path = _stop_request_path()
        stop_request_path.parent.mkdir(parents=True, exist_ok=True)
        stop_request_path.unlink(missing_ok=True)
        command = scan_command(tuning_options)
        self.profile_disabled_for_scan = bool(_systemd_autostart_profile_selector())
        self.runs_table.clear()
        self.vf_plot.clear()
        self.fan_measured_points = []
        self.fan_plot.clear()
        self.controls.hide_dependency_progress()
        self.log_view.append("$ " + " ".join(command) + "\n")

        process = self.QtCore.QProcess(self.window)
        process.setProcessChannelMode(self.QtCore.QProcess.MergedChannels)
        process.readyReadStandardOutput.connect(self._read_process_output)
        process.finished.connect(self._process_finished)
        self.process = process
        self.active_stop_request_path = stop_request_path
        self.stop_requested = False
        self.last_auto_uv_candidate_id = ""
        self.last_auto_uv_profile_id = ""
        self.pending_final_result_payload = None
        self.final_choice_discarded = False

        self.header.set_stage("Starting")
        self.header.set_candidate("Writing to main Auto-UV profile store")
        if self.profile_disabled_for_scan:
            self.controls.set_status_text(
                "Profile disabled during the auto undervolting sweep"
            )
        self.controls.set_running(True)
        self.profile_list.set_runtime_actions_enabled(False)
        process.start(command[0], command[1:])
        if not process.waitForStarted(3000):
            self.log_view.append("Failed to start Auto-UV process.\n")
            self._process_finished(-1, self.QtCore.QProcess.CrashExit)

    def _import_afterburner_profile(self) -> None:
        if (
            self.process is not None
            or self.runtime_process is not None
            or self.profile_delete_process is not None
        ):
            self.controls.set_status_text(
                "Finish the current PenguinBurner action before importing Afterburner."
            )
            return
        try:
            entry = self._select_afterburner_import_dialog()
        except Exception as exc:
            self.controls.set_status_text("Afterburner import failed.")
            self.log_view.append(f"\nAfterburner import failed: {exc}\n")
            self._show_error_dialog(
                "Import Afterburner",
                f"Afterburner import failed:\n{exc}",
            )
            return
        if entry is None:
            return
        try:
            result = _persist_afterburner_import_selection(entry)
        except Exception as exc:
            self.controls.set_status_text("Afterburner import failed.")
            self.log_view.append(f"\nAfterburner import failed: {exc}\n")
            self._show_error_dialog(
                "Import Afterburner",
                f"Afterburner import failed:\n{exc}",
            )
            return

        message = (
            "Imported Afterburner profile "
            f"{result['section']} from {Path(result['device_profile_relative_path']).name}."
        )
        self.controls.set_status_text(message)
        self.log_view.append(
            "\n"
            + message
            + f"\nManaged Afterburner directory: {result['afterburner_root']}\n"
        )
        self._load_profiles()
        self.tabs.setCurrentIndex(self.profiles_tab_index)
        self.profile_list.select_profile(AFTERBURNER_PROFILE_ID)
        self.profile_list.table.setFocus()
        self.QtWidgets.QMessageBox.information(
            self.window,
            "Import Afterburner",
            message,
        )

    def _select_afterburner_import_dialog(self) -> dict | None:
        dialog = self.QtWidgets.QDialog(self.window)
        dialog.setWindowTitle("Import Afterburner")
        dialog.resize(1040, 560)
        layout = self.QtWidgets.QVBoxLayout(dialog)

        directory_row = self.QtWidgets.QHBoxLayout()
        directory_label = self.QtWidgets.QLabel("Afterburner directory")
        directory_edit = self.QtWidgets.QLineEdit(_configured_afterburner_root())
        browse_button = self.QtWidgets.QToolButton()
        standard_pixmap = getattr(
            self.QtWidgets.QStyle,
            "StandardPixmap",
            self.QtWidgets.QStyle,
        )
        browse_button.setIcon(
            dialog.style().standardIcon(getattr(standard_pixmap, "SP_DirOpenIcon"))
        )
        browse_button.setToolTip("Choose Afterburner Directory")
        browse_button.setAccessibleName("Choose Afterburner Directory")
        directory_row.addWidget(directory_label)
        directory_row.addWidget(directory_edit, 1)
        directory_row.addWidget(browse_button)

        table = self.QtWidgets.QTableWidget(0, 4)
        table.setHorizontalHeaderLabels(
            ["Device Profile", "Afterburner Profile", "Target", "Status"]
        )
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(self.QtWidgets.QAbstractItemView.SelectRows)
        table.setSelectionMode(self.QtWidgets.QAbstractItemView.SingleSelection)
        table.setEditTriggers(self.QtWidgets.QAbstractItemView.NoEditTriggers)
        table.setSortingEnabled(False)
        set_header_fit_column_widths(
            table,
            {
                0: 260,
                1: 150,
                2: 145,
                3: 220,
            },
            QtCore=self.QtCore,
            padding=32,
        )
        table.horizontalHeader().setStretchLastSection(True)

        preview_plot = CurvePlot(
            QtWidgets=self.QtWidgets,
            pg=self.pg,
            x_label="Voltage",
            x_units="mV",
            y_label="Clock",
            y_units="MHz",
            x_range=(700, 1250),
            y_range=(1000, 3200),
            source_name="Base",
            candidate_name="Imported",
            show_source=False,
        )
        preview_plot.enable_point_selection(True)

        splitter = self.QtWidgets.QSplitter(self.QtCore.Qt.Horizontal)
        splitter.addWidget(table)
        splitter.addWidget(preview_plot.widget)
        splitter.setSizes([470, 570])

        status_label = self.QtWidgets.QLabel("")
        status_label.setWordWrap(True)
        buttons = self.QtWidgets.QDialogButtonBox()
        import_button = buttons.addButton(
            "Import",
            self.QtWidgets.QDialogButtonBox.AcceptRole,
        )
        buttons.addButton(self.QtWidgets.QDialogButtonBox.Cancel)
        import_button.setEnabled(False)

        layout.addLayout(directory_row)
        layout.addWidget(splitter, 1)
        layout.addWidget(status_label)
        layout.addWidget(buttons)

        entries: list[dict] = []
        chosen: dict[str, dict | None] = {"entry": None}
        role = 257

        def selected_entry() -> dict | None:
            rows = table.selectionModel().selectedRows()
            if not rows:
                return None
            item = table.item(int(rows[-1].row()), 0)
            if item is None:
                return None
            try:
                index = int(item.data(role))
            except (TypeError, ValueError):
                return None
            if index < 0 or index >= len(entries):
                return None
            return entries[index]

        def sync_selection_state() -> None:
            entry = selected_entry()
            importable = bool(entry and entry.get("importable"))
            import_button.setEnabled(importable)
            preview_plot.clear()
            if entry:
                points = _entry_curve_points(entry)
                preview_plot.set_candidate_points(points, remember_previous=False)
                if entry.get("target_voltage_mv") and entry.get("target_clock_mhz"):
                    preview_plot.set_selected_point(
                        entry.get("target_voltage_mv"),
                        entry.get("target_clock_mhz"),
                    )
            if entry and not importable:
                status_label.setText(str(entry.get("status", "")))

        def add_cell(row: int, column: int, text: str, entry_index: int) -> None:
            item = self.QtWidgets.QTableWidgetItem(str(text))
            item.setData(role, int(entry_index))
            if not entries[entry_index].get("importable"):
                item.setForeground(self.QtGui.QColor("#7f8794"))
            table.setItem(row, column, item)

        def populate_profiles() -> None:
            root_text = str(directory_edit.text()).strip()
            entries.clear()
            table.setRowCount(0)
            import_button.setEnabled(False)
            chosen["entry"] = None
            if not root_text:
                status_label.setText("Choose an MSI Afterburner directory.")
                return
            try:
                entries.extend(_afterburner_profile_entries(root_text))
            except Exception as exc:
                status_label.setText(str(exc))
                return
            if not entries:
                status_label.setText(
                    "No saved Afterburner V/F profiles were found in that directory."
                )
                return
            for entry_index, entry in enumerate(entries):
                row = table.rowCount()
                table.insertRow(row)
                add_cell(row, 0, entry["device_profile_name"], entry_index)
                add_cell(row, 1, entry["section"], entry_index)
                add_cell(row, 2, entry["target"], entry_index)
                add_cell(row, 3, entry["status"], entry_index)
                if entry.get("importable"):
                    for column in range(table.columnCount()):
                        font = table.item(row, column).font()
                        font.setBold(True)
                        table.item(row, column).setFont(font)
            first_importable_row = next(
                (
                    row
                    for row, entry in enumerate(entries)
                    if bool(entry.get("importable"))
                ),
                None,
            )
            if first_importable_row is not None:
                table.selectRow(first_importable_row)
                status_label.setText(
                    "Select one Afterburner profile to import into PenguinBurner."
                )
            else:
                status_label.setText(
                    "Afterburner profiles were found, but none are importable."
                )
            sync_selection_state()

        def browse_directory() -> None:
            selected = self.QtWidgets.QFileDialog.getExistingDirectory(
                dialog,
                "Choose Afterburner Directory",
                str(directory_edit.text()).strip() or str(Path.home()),
            )
            if selected:
                directory_edit.setText(selected)
                populate_profiles()

        def accept_import() -> None:
            entry = selected_entry()
            if not entry or not entry.get("importable"):
                status_label.setText("Select one importable Afterburner profile.")
                return
            chosen["entry"] = dict(entry)
            dialog.accept()

        browse_button.clicked.connect(browse_directory)
        directory_edit.editingFinished.connect(populate_profiles)
        table.itemSelectionChanged.connect(sync_selection_state)
        buttons.accepted.connect(accept_import)
        buttons.rejected.connect(dialog.reject)
        populate_profiles()

        if dialog.exec() != self.QtWidgets.QDialog.Accepted:
            return None
        entry = chosen.get("entry")
        return dict(entry) if isinstance(entry, dict) else None

    def _select_auto_uv_tuning_dialog(self) -> dict[str, object] | None:
        dialog = self.QtWidgets.QDialog(self.window)
        dialog.setWindowTitle("Automatic undervolt behavior")
        layout = self.QtWidgets.QVBoxLayout(dialog)

        purpose = self.QtWidgets.QLabel(GPU_UNDERVOLTING_PURPOSE_TEXT)
        purpose.setObjectName("purposeText")
        purpose.setWordWrap(True)
        purpose.setAlignment(
            _qt_flags(self.QtCore.Qt, "AlignmentFlag", "AlignLeft", "AlignVCenter")
        )

        def wrapped_tooltip(text: str) -> str:
            normalized = " ".join(str(text).split())
            escaped = html.escape(normalized)
            return f"<qt><table width='680'><tr><td>{escaped}</td></tr></table></qt>"

        bias_group = self.QtWidgets.QGroupBox("Performance bias")
        bias_layout = self.QtWidgets.QVBoxLayout(bias_group)
        performance_bias_slider = _click_jump_slider_class(
            self.QtCore,
            self.QtWidgets,
        )(self.QtCore.Qt.Horizontal)
        performance_bias_slider.setRange(0, 100)
        performance_bias_slider.setSingleStep(1)
        performance_bias_slider.setPageStep(5)
        performance_bias_slider.setToolTip(wrapped_tooltip(PERFORMANCE_BIAS_TOOLTIP_TEXT))
        performance_bias_slider.setToolTipDuration(20000)

        def performance_bias_max_pct() -> float:
            return (
                float(YOLO_MAX_OVERCLOCK_BUDGET_PCT)
                if bool(self.auto_uv_yolo)
                else float(MAX_OVERCLOCK_BUDGET_PCT)
            )

        def performance_bias_slider_style(max_pct: float) -> str:
            warning_stop = (
                _performance_bias_slider_position(
                    PERFORMANCE_BIAS_WARNING_PCT,
                    max_pct=float(max_pct),
                )
                / 100.0
            )
            danger_stop = (
                _performance_bias_slider_position(
                    PERFORMANCE_BIAS_DANGER_PCT,
                    max_pct=float(max_pct),
                )
                / 100.0
            )
            return f"""
            QSlider::groove:horizontal {{
                height: 7px;
                border-radius: 3px;
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 1, y2: 0,
                    stop: 0 #55d27a,
                    stop: {warning_stop:.2f} #55d27a,
                    stop: {warning_stop:.2f} #f0b84c,
                    stop: {danger_stop:.2f} #f0b84c,
                    stop: {danger_stop:.2f} #ff6b6b,
                    stop: 1.00 #ff6b6b
                );
            }}
            QSlider::handle:horizontal {{
                background: #f2f5f2;
                border: 1px solid #10140f;
                width: 18px;
                margin: -7px 0;
                border-radius: 9px;
            }}
            """
        performance_bias_slider.setStyleSheet(
            performance_bias_slider_style(performance_bias_max_pct())
        )

        def bias_icon(filename: str, tooltip: str):
            label = self.QtWidgets.QLabel()
            label.setFixedSize(56, 56)
            label.setAlignment(self.QtCore.Qt.AlignCenter)
            label.setToolTip(tooltip)
            path = _asset_image_path(filename)
            if path is not None:
                pixmap = self.QtGui.QPixmap(str(path))
                if not pixmap.isNull():
                    aspect_mode = getattr(
                        getattr(self.QtCore.Qt, "AspectRatioMode", self.QtCore.Qt),
                        "KeepAspectRatio",
                    )
                    transform_mode = getattr(
                        getattr(self.QtCore.Qt, "TransformationMode", self.QtCore.Qt),
                        "SmoothTransformation",
                    )
                    label.setPixmap(
                        pixmap.scaled(54, 54, aspect_mode, transform_mode)
                    )
            return label

        slider_column = self.QtWidgets.QVBoxLayout()
        slider_column.setContentsMargins(0, 0, 0, 0)
        slider_column.setSpacing(5)
        bias_labels = self.QtWidgets.QHBoxLayout()
        efficiency_label = self.QtWidgets.QLabel("Efficiency")
        performance_label = self.QtWidgets.QLabel("Performance")
        performance_label.setAlignment(
            _qt_flags(self.QtCore.Qt, "AlignmentFlag", "AlignRight", "AlignVCenter")
        )
        bias_labels.addWidget(efficiency_label)
        bias_labels.addStretch(1)
        bias_labels.addWidget(performance_label)
        slider_column.addWidget(performance_bias_slider)
        slider_column.addLayout(bias_labels)

        bias_control_layout = self.QtWidgets.QHBoxLayout()
        bias_control_layout.setContentsMargins(0, 0, 0, 0)
        bias_control_layout.setSpacing(12)
        bias_control_layout.addWidget(
            bias_icon("penguin-burner-green.png", "Efficiency")
        )
        bias_control_layout.addLayout(slider_column, 1)
        bias_control_layout.addWidget(bias_icon("penguin-burner.png", "Performance"))
        bias_layout.addLayout(bias_control_layout)

        advanced_group = self.QtWidgets.QGroupBox("Advanced")
        form = self.QtWidgets.QFormLayout(advanced_group)
        form.setFieldGrowthPolicy(self.QtWidgets.QFormLayout.FieldsStayAtSizeHint)
        max_drop_spin = self.QtWidgets.QDoubleSpinBox()
        max_drop_spin.setRange(1.0, 30.0)
        max_drop_spin.setDecimals(1)
        max_drop_spin.setSuffix("%")
        max_drop_spin.setFixedWidth(96)
        max_clock_drop_spin = self.QtWidgets.QDoubleSpinBox()
        max_clock_drop_spin.setRange(1.0, 30.0)
        max_clock_drop_spin.setDecimals(1)
        max_clock_drop_spin.setSuffix("%")
        max_clock_drop_spin.setFixedWidth(96)
        short_seconds_spin = self.QtWidgets.QSpinBox()
        short_seconds_spin.setRange(10, 60)
        short_seconds_spin.setSuffix(" sec")
        short_seconds_spin.setSingleStep(5)
        short_seconds_spin.setFixedWidth(110)
        memory_offset_spin = self.QtWidgets.QSpinBox()
        memory_offset_min_mhz, memory_offset_max_mhz = _memory_offset_mhz_range()
        memory_offset_spin.setRange(
            int(memory_offset_min_mhz),
            int(memory_offset_max_mhz),
        )
        memory_offset_spin.setSuffix(" MHz")
        memory_offset_spin.setSingleStep(50)
        memory_offset_spin.setFixedWidth(118)

        voltage_tooltip = wrapped_tooltip(
            "How deep Auto-UV may go below the starting voltage. Higher values "
            "search for lower power, but may spend more time near unstable "
            "voltage bins. Changing this can result in instability; modify with care."
        )
        clock_drop_tooltip = wrapped_tooltip(
            "How much loaded frequency degradation Auto-UV may accept while "
            "lowering voltage. Higher values allow deeper undervolts with more "
            "performance loss. Changing this can result in instability; modify with care."
        )
        memory_offset_tooltip = wrapped_tooltip(
            "Optional global memory clock V/F offset in MHz applied during the "
            "Auto-UV scan and saved with the final profile. Higher values can "
            "improve memory performance, but may introduce instability or be "
            "rejected by the Nvidia driver; modify with care."
        )

        def add_form_row(
            form_layout,
            text: str,
            widget,
            tooltip: str = "",
        ) -> None:
            label_widget = self.QtWidgets.QLabel(text)
            if tooltip:
                label_widget.setToolTip(tooltip)
                widget.setToolTip(tooltip)
                widget.setToolTipDuration(20000)
                label_container = self.QtWidgets.QWidget()
                label_layout = self.QtWidgets.QHBoxLayout(label_container)
                label_layout.setContentsMargins(0, 0, 0, 0)
                label_layout.setSpacing(6)
                info_button = self.QtWidgets.QToolButton()
                info_button.setObjectName("infoButton")
                info_button.setText("i")
                info_button.setToolTip(tooltip)
                info_button.setToolTipDuration(20000)
                info_button.setCursor(self.QtCore.Qt.WhatsThisCursor)
                info_button.setFocusPolicy(self.QtCore.Qt.NoFocus)
                info_button.setFixedSize(18, 18)

                def show_tooltip(_checked=False, *, button=info_button, tip=tooltip):
                    position = button.mapToGlobal(button.rect().bottomLeft())
                    self.QtWidgets.QToolTip.showText(position, tip, button)

                info_button.clicked.connect(show_tooltip)
                label_layout.addWidget(label_widget)
                label_layout.addWidget(info_button)
                label_layout.addStretch(1)
                form_layout.addRow(label_container, widget)
                return
            form_layout.addRow(label_widget, widget)

        add_form_row(form, "Max voltage drop", max_drop_spin, voltage_tooltip)
        add_form_row(form, "Max clock drop", max_clock_drop_spin, clock_drop_tooltip)
        add_form_row(
            form,
            "Memory Offset MHz",
            memory_offset_spin,
            memory_offset_tooltip,
        )
        add_form_row(form, "Base verification length", short_seconds_spin)

        performance_bias_slider.setValue(
            _performance_bias_slider_position(DEFAULT_AUTO_UV_PERFORMANCE_BIAS_PCT)
        )
        max_drop_spin.setValue(DEFAULT_AUTO_UV_MAX_DROP_PCT)
        max_clock_drop_spin.setValue(DEFAULT_AUTO_UV_MAX_CLOCK_DROP_PCT)
        memory_offset_spin.setValue(0)
        short_seconds_spin.setValue(DEFAULT_SHORT_VERIFICATION_BASE_S)

        buttons = self.QtWidgets.QDialogButtonBox()
        start_button = buttons.addButton(
            "Start Auto Undervolt",
            self.QtWidgets.QDialogButtonBox.AcceptRole,
        )
        buttons.addButton(self.QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        start_button.setDefault(True)

        layout.addWidget(purpose)
        layout.addWidget(bias_group)
        layout.addWidget(advanced_group)
        layout.addWidget(buttons)
        dialog.setMinimumWidth(520)

        if dialog.exec() != self.QtWidgets.QDialog.Accepted:
            return None
        performance_bias_pct = _performance_bias_clock_recovery_pct(
            performance_bias_slider.value(),
            max_pct=performance_bias_max_pct(),
        )
        return {
            "auto_uv_mode": _auto_uv_mode_for_performance_bias(performance_bias_pct),
            "auto_uv_max_drop_pct": float(max_drop_spin.value()),
            "auto_uv_max_clock_drop_pct": float(max_clock_drop_spin.value()),
            "auto_uv_clock_bump_budget_ratio": performance_bias_pct / 100.0,
            "auto_uv_yolo": bool(self.auto_uv_yolo),
            "auto_uv_memory_offset_mhz": int(memory_offset_spin.value()),
            "auto_uv_short_seconds": int(short_seconds_spin.value()),
        }

    def stop_scan(self) -> None:
        if self.process is None:
            if (
                self.runtime_process is not None
                and self.runtime_process_kind == "reverify"
            ):
                self.reverify_stop_requested = True
                self.controls.set_status_text("Stopping profile verification.")
                stop_path = self.active_reverify_stop_request_path
                if stop_path is not None:
                    stop_path.parent.mkdir(parents=True, exist_ok=True)
                    stop_path.write_text(
                        "stop requested by PenguinBurner UI\n",
                        encoding="utf-8",
                    )
                    self.log_view.append(
                        f"\nRequested cooperative profile verification stop: {stop_path}\n"
                    )
                target = max(1, int(self.reverify_target_duration_s or 1))
                elapsed = max(0.0, float(self.reverify_last_elapsed_s))
                self.controls.set_reverify_progress(
                    _reverify_progress_percent(elapsed, target),
                    elapsed_s=elapsed,
                    target_s=target,
                    detail="Stopping profile verification.",
                )
                pid = int(self.runtime_process.processId())
                if pid > 0:
                    try:
                        os.kill(pid, signal.SIGINT)
                        self.log_view.append(
                            "Sent SIGINT to profile verification launcher.\n"
                        )
                    except OSError:
                        self.runtime_process.terminate()
                else:
                    self.runtime_process.terminate()
                self.QtCore.QTimer.singleShot(30000, self._kill_runtime_if_running)
            return
        self.stop_requested = True
        self.runs_table.mark_running_rows_stopping()
        stop_path = self.active_stop_request_path
        if stop_path is not None:
            stop_path.parent.mkdir(parents=True, exist_ok=True)
            stop_path.write_text("stop requested by PenguinBurner UI\n", encoding="utf-8")
            self.log_view.append(
                f"\nRequested cooperative Auto-UV stop: {stop_path}\n"
            )
        self.header.set_stage("Stopping")
        pid = int(self.process.processId())
        if pid > 0:
            try:
                os.kill(pid, signal.SIGINT)
                self.log_view.append("Sent SIGINT to Auto-UV launcher.\n")
            except OSError:
                self.process.terminate()
        else:
            self.process.terminate()
        self.QtCore.QTimer.singleShot(30000, self._kill_if_running)

    def _kill_runtime_if_running(self) -> None:
        if (
            self.runtime_process is not None
            and self.runtime_process.state() != self.QtCore.QProcess.NotRunning
        ):
            self.log_view.append(
                "\nProfile verification did not stop after request; "
                "terminating process.\n"
            )
            self.runtime_process.kill()

    def _kill_if_running(self) -> None:
        if (
            self.process is not None
            and self.process.state() != self.QtCore.QProcess.NotRunning
        ):
            self.log_view.append(
                "\nAuto-UV did not stop after the cooperative request; "
                "terminating launcher.\n"
            )
            self.process.kill()

    def _read_process_output(self) -> None:
        if self.process is None:
            return
        data = bytes(self.process.readAllStandardOutput()).decode(
            "utf-8",
            errors="replace",
        )
        self.log_view.append(data)
        for line in data.splitlines():
            self._handle_line(line)

    def _handle_line(self, line: str) -> None:
        stripped = line.strip()
        if not stripped.startswith("{"):
            self._handle_human_line(stripped)
            return
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            return
        event = str(payload.get("event", ""))
        if event == "auto_uv_start":
            self.header.set_stage("Scanning")
        elif event == "dependency_progress":
            self._handle_dependency_progress(payload)
        elif event == "probe_start":
            self.controls.hide_dependency_progress()
            self.header.set_stage(_stage_title(payload.get("stage", "Probe")))
            voltage = payload.get("voltage_mv")
            clock = payload.get("clock_mhz")
            self.header.set_candidate(
                f"Running {_status_value(voltage) or 'n/a'} mV @ "
                f"{_status_value(clock) or 'n/a'} MHz"
            )
            self.runs_table.add_probe_start(payload)
            self.vf_plot.set_probe_marker(payload)
        elif event == "probe_result":
            self.header.set_stage(_stage_title(payload.get("stage", "Probe")))
            self.runs_table.add_probe_result(payload)
            self.vf_plot.set_load_markers(payload)
            self._record_fan_measurement(payload)
        elif event == "load_telemetry":
            self.runs_table.update_probe_progress(payload)
            self.vf_plot.set_live_load_marker(payload)
        elif event == "final_choice_request":
            self._handle_final_choice_request(payload)
        elif event == "final_choice_discarded":
            self.final_choice_discarded = True
            self.header.set_stage("Discarded")
            self.header.set_candidate("")
            self.controls.set_status_text(
                "Final verification discarded. No Auto-UV profile was saved."
            )
        elif event == "source_curve":
            self.controls.hide_dependency_progress()
            points = _event_base_points(payload)
            self.vf_plot.set_source_points(points)
            self.base_curve_points = points
            _save_cached_base_curve_points(points)
        elif event == "candidate_curve":
            self.vf_plot.set_candidate_points(
                _event_points(payload),
                curve_id=_candidate_id_from_result(payload),
            )
        elif event == "fan_curve_suggested":
            self._record_fan_measurements(payload.get("measured_points", []))
            self.fan_plot.set_candidate_points(_fan_points(payload))
        elif event == "final_result":
            self.header.set_stage("Complete")
            voltage = payload.get("voltage_mv")
            clock = payload.get("clock_mhz")
            self.header.set_candidate(
                f"{_status_value(voltage) or 'n/a'} mV @ "
                f"{_status_value(clock) or 'n/a'} MHz"
            )
            result_candidate_id = _candidate_id_from_result(payload)
            if result_candidate_id:
                self.last_auto_uv_candidate_id = result_candidate_id
            self.pending_final_result_payload = dict(payload)
            self._load_profiles(prefer_last_auto_uv=True)

    def _handle_runs_table_candidate_selection(
        self,
        candidate_id: str | None,
    ) -> None:
        self.vf_plot.set_highlighted_curve(candidate_id)

    def _handle_human_line(self, line: str) -> None:
        if not line:
            return
        lower = line.lower()
        if "final verification" in lower:
            self.header.set_stage("Final verification")
        elif "profile disabled during the auto undervolting sweep" in lower:
            self.profile_disabled_for_scan = True
            self.controls.set_status_text(
                "Profile disabled during the auto undervolting sweep"
            )
        elif "candidate" in lower and "mv" in lower:
            self.header.set_stage("Undervolting Candidates Sweep")
            self.header.set_candidate(_top_status_text(line))
        elif "auto-uv final state" in lower:
            self.header.set_stage("Complete")
            self.header.set_candidate(_top_status_text(line))

    def _handle_dependency_progress(self, payload: dict) -> None:
        detail = str(payload.get("detail") or "Downloading dependencies").strip()
        percent = payload.get("percent", 0)
        self.header.set_stage("Downloading dependencies")
        self.header.set_candidate("")
        if not self.profile_disabled_for_scan:
            self.controls.set_status_text(
                _round_gui_decimals(detail) or "Downloading dependencies"
            )
        self.controls.set_dependency_progress(percent, detail=detail)

    def _tab_changed(self, index: int) -> None:
        if int(index) == int(self.profiles_tab_index):
            self._load_profiles(prefer_last_auto_uv=True)

    def _refresh_profiles_if_visible(self) -> None:
        if int(self.tabs.currentIndex()) != int(self.profiles_tab_index):
            return
        self._load_profiles(prefer_last_auto_uv=True)

    def _close_curve_tab(self, index: int) -> None:
        widget = self.tabs.widget(int(index))
        if widget is None or self._is_fixed_tab_widget(widget):
            self._sync_tab_close_buttons()
            return
        self.tabs.removeTab(int(index))
        for key, curve_widget in list(self.profile_curve_widgets.items()):
            if curve_widget is widget:
                del self.profile_curve_widgets[key]
        if hasattr(widget, "deleteLater"):
            widget.deleteLater()
        self._sync_tab_close_buttons()

    def _sync_tab_close_buttons(self) -> None:
        tab_bar = self.tabs.tabBar()
        for index in range(self.tabs.count()):
            if not self._is_fixed_tab_widget(self.tabs.widget(index)):
                continue
            for position in self._tab_button_positions():
                tab_bar.setTabButton(index, position, None)

    def _tab_button_positions(self) -> list:
        position_enum = getattr(
            self.QtWidgets.QTabBar,
            "ButtonPosition",
            self.QtWidgets.QTabBar,
        )
        positions = []
        for name in ("LeftSide", "RightSide"):
            position = getattr(position_enum, name, None)
            if position is not None:
                positions.append(position)
        return positions

    def _is_fixed_tab_widget(self, widget) -> bool:
        return any(widget is fixed_widget for fixed_widget in self.fixed_tab_widgets)

    def _load_profiles(
        self,
        *,
        prefer_last_auto_uv: bool = False,
        select_last_auto_uv: bool = False,
        preserve_persist_toggle: bool = True,
    ) -> None:
        profiles = read_auto_uv_profile_summaries()
        afterburner_profile = _afterburner_import_profile_summary()
        if afterburner_profile is not None:
            profiles.append(afterburner_profile)
        self.profile_summaries = profiles
        has_systemd_entry = _systemd_unit_entry_exists()
        systemd_selector = _systemd_autostart_profile_selector()
        if systemd_selector in {"active", "latest", "__systemd_default__"} and profiles:
            systemd_selector = str(profiles[0].get("profile_id", ""))
        use_last_auto_uv = bool(prefer_last_auto_uv and select_last_auto_uv)
        preferred_candidate_id = (
            self.last_auto_uv_candidate_id if use_last_auto_uv else ""
        )
        preferred_profile_id = self.last_auto_uv_profile_id if use_last_auto_uv else ""
        if preferred_candidate_id and not preferred_profile_id:
            preferred_profile_id = _profile_id_for_candidate(
                profiles,
                preferred_candidate_id,
            )
            if preferred_profile_id:
                self.last_auto_uv_profile_id = preferred_profile_id
        self.profile_list.set_profiles(
            profiles,
            systemd_selector=systemd_selector,
            has_systemd_entry=has_systemd_entry,
            preferred_candidate_id=preferred_candidate_id,
            preferred_profile_id=preferred_profile_id,
            select_preferred=use_last_auto_uv,
            preserve_persist_toggle=preserve_persist_toggle,
        )
        self._update_runner_status()

    def _show_profile_context_menu(self, position) -> None:
        table = self.profile_list.table
        index = table.indexAt(position)
        if not index.isValid():
            return
        profile = self._profile_from_table_index(index)
        if profile is None:
            return
        table.selectRow(int(index.row()))
        menu = self.QtWidgets.QMenu(table)

        def add_menu_action_once(text: str):
            for action in menu.actions():
                if action.text() == text:
                    return action
            return menu.addAction(text)

        edit_vf_action = add_menu_action_once("Edit VF Curve")
        edit_vf_action.setEnabled(bool(_profile_curve_plan(profile)))
        view_fan_action = add_menu_action_once("Edit Fan Curve")
        view_fan_action.setEnabled(bool(_profile_fan_curve_points(profile)))
        apply_action = add_menu_action_once("Apply")
        apply_action.setEnabled(
            self._profile_actions_available() and _profile_can_apply(profile)
        )
        reverify_action = add_menu_action_once("Verify")
        reverify_action.setEnabled(
            self._profile_actions_available() and _profile_can_reverify(profile)
        )
        export_lact_action = add_menu_action_once("Export LACT")
        export_lact_action.setEnabled(
            self._profile_actions_available() and _profile_can_export_lact(profile)
        )
        menu.addSeparator()
        delete_action = add_menu_action_once("Delete")
        delete_action.setEnabled(
            self._profile_actions_available() and _profile_is_deletable(profile)
        )
        chosen = menu.exec(table.viewport().mapToGlobal(position))
        if chosen == edit_vf_action:
            self._open_profile_curve_editor(profile)
        elif chosen == view_fan_action:
            self._open_profile_fan_curve_editor(profile)
        elif chosen == apply_action:
            self._apply_profile(profile)
        elif chosen == reverify_action:
            self._reverify_profile(profile)
        elif chosen == export_lact_action:
            self._export_lact_profile(profile)
        elif chosen == delete_action:
            self._delete_selected_profiles()

    def _view_profile_curve_from_index(self, index) -> None:
        profile = self._profile_from_table_index(index)
        if profile is None:
            return
        self._open_profile_curve_tab(profile)

    def _profile_from_table_index(self, index) -> dict | None:
        if index is None or not index.isValid():
            return None
        row = int(index.row())
        item = self.profile_list.table.item(row, 0)
        if item is None:
            return None
        profile_id = str(item.data(self.profile_list.PROFILE_ID_ROLE) or "").strip()
        return _profile_for_selector(self.profile_summaries, profile_id)

    def _open_profile_curve_tab(self, profile: dict) -> None:
        points = _profile_curve_points(profile)
        if not points:
            self.controls.set_status_text("No curve points are available for this profile.")
            self.QtWidgets.QMessageBox.information(
                self.window,
                "View VF Curve",
                "No curve points are available for this profile.",
            )
            return
        key = _profile_curve_tab_key(profile)
        widget = self.profile_curve_widgets.get(key)
        if widget is not None:
            index = self.tabs.indexOf(widget)
            if index >= 0:
                self.tabs.setCurrentIndex(index)
                return
        base_points = _profile_base_curve_points(profile) or self.base_curve_points

        plot = CurvePlot(
            QtWidgets=self.QtWidgets,
            pg=self.pg,
            x_label="Voltage",
            x_units="mV",
            y_label="Clock",
            y_units="MHz",
            x_range=(700, 1250),
            y_range=(1000, 3200),
            source_name="Base",
            candidate_name=_profile_curve_legend_label(profile),
            show_source=bool(base_points),
        )
        plot.enable_point_selection(True)
        if base_points:
            plot.set_source_points(base_points)
        plot.set_candidate_points(points, remember_previous=False)
        target = _profile_curve_target_point(profile)
        if target is not None:
            plot.set_selected_point(target[0], target[1])
        label = _profile_curve_tab_label(profile)
        tab_index = self.tabs.addTab(plot.widget, label)
        self.profile_curve_widgets[key] = plot.widget
        self._sync_tab_close_buttons()
        self.tabs.setCurrentIndex(tab_index)

    def _open_profile_curve_editor(self, profile: dict) -> None:
        if self.pg is None:
            self.QtWidgets.QMessageBox.information(
                self.window,
                "Edit VF Curve",
                "pyqtgraph is not installed, so the curve editor is unavailable.",
            )
            return
        plan = _profile_curve_plan(profile)
        anchor = editable_anchor_from_profile(profile)
        if not plan or anchor is None:
            self.controls.set_status_text("No editable V/F curve is available.")
            self.QtWidgets.QMessageBox.information(
                self.window,
                "Edit VF Curve",
                "No editable V/F curve is available for this profile.",
            )
            return
        original_anchor_voltage_mv, original_anchor_clock_mhz = anchor
        parent_payload = _profile_payload_from_path(profile) or dict(profile)
        base_points = _profile_base_curve_points(profile) or self.base_curve_points
        initial_curve_points = _curve_points_from_values(plan)

        def initial_manual_edit() -> ManualCurveEdit:
            return ManualCurveEdit(
                plan=[dict(item) for item in plan],
                anchor_voltage_mv=int(original_anchor_voltage_mv),
                anchor_clock_mhz=int(original_anchor_clock_mhz),
                edit_kind="unchanged",
            )

        current_edit = {"value": initial_manual_edit()}
        undo_stack: list[ManualCurveEdit] = []
        redo_stack: list[ManualCurveEdit] = []
        active_undo_actions: dict[str, ManualCurveEdit] = {}
        syncing_target = {"active": False}
        syncing_left_points = {"active": False}
        left_point_items = {}

        dialog = self.QtWidgets.QDialog(self.window)
        dialog.setWindowTitle("Edit VF Curve")
        dialog.resize(900, 640)
        layout = self.QtWidgets.QVBoxLayout(dialog)

        plot = CurvePlot(
            QtWidgets=self.QtWidgets,
            pg=self.pg,
            x_label="Voltage",
            x_units="mV",
            y_label="Clock",
            y_units="MHz",
            x_range=(700, 1250),
            y_range=(1000, 3200),
            source_name="Reference",
            candidate_name="Edited draft",
            show_source=bool(base_points),
            source_color="#9aa1aa",
            source_alpha=204,
            candidate_color="#5ef38c",
        )
        if base_points:
            plot.set_source_points(base_points)
        plot.add_comparison_points(
            initial_curve_points,
            name="Before edit",
            color="#dfe4ea",
            alpha=145,
            width=1,
        )
        plot.set_candidate_points(initial_curve_points, remember_previous=False)
        plot.set_selected_point(original_anchor_voltage_mv, original_anchor_clock_mhz)
        layout.addWidget(plot.widget, 1)

        target_item = self.pg.TargetItem(
            pos=(float(original_anchor_voltage_mv), float(original_anchor_clock_mhz)),
            size=14,
            symbol="o",
            pen=self.pg.mkPen(self.pg.mkColor(255, 229, 177, 240), width=1),
            brush=self.pg.mkBrush(self.pg.mkColor(240, 184, 76, 235)),
            movable=True,
        )
        plot.plot.addItem(target_item)
        selected_range_overlay = plot.plot.plot(
            [],
            [],
            pen=None,
            symbol="o",
            symbolPen=self.pg.mkPen(self.pg.mkColor(255, 229, 177, 245), width=1),
            symbolBrush=self.pg.mkBrush(self.pg.mkColor(240, 184, 76, 225)),
            symbolSize=8,
        )
        if hasattr(selected_range_overlay, "setZValue"):
            selected_range_overlay.setZValue(8)

        legend_overlay = self.QtWidgets.QFrame(plot.widget)
        legend_overlay.setObjectName("curveEditorShortcutLegend")
        styled_background = getattr(
            getattr(self.QtCore.Qt, "WidgetAttribute", self.QtCore.Qt),
            "WA_StyledBackground",
            None,
        )
        if styled_background is not None:
            legend_overlay.setAttribute(styled_background, True)
        legend_overlay.setStyleSheet(
            """
            QFrame#curveEditorShortcutLegend {
                background: rgba(12, 17, 23, 184);
                border: 1px solid rgba(140, 156, 172, 92);
                border-radius: 7px;
            }
            QLabel#curveEditorShortcutTitle {
                color: #e7edf4;
                font-size: 10px;
                font-weight: 700;
            }
            QLabel#curveEditorShortcutKey {
                color: #dff6e8;
                background: rgba(94, 243, 140, 36);
                border: 1px solid rgba(94, 243, 140, 72);
                border-radius: 4px;
                padding: 1px 4px;
                font-size: 9px;
                font-weight: 700;
            }
            QLabel#curveEditorShortcutText {
                color: #bdc7d3;
                font-size: 9px;
            }
            QToolButton#curveEditorShortcutToggle {
                color: #e7edf4;
                background: rgba(255, 255, 255, 18);
                border: 1px solid rgba(255, 255, 255, 34);
                border-radius: 4px;
                padding: 0px 4px;
                font-size: 10px;
                font-weight: 700;
            }
            QToolButton#curveEditorShortcutToggle:hover {
                background: rgba(94, 243, 140, 38);
            }
            """
        )
        legend_layout = self.QtWidgets.QVBoxLayout(legend_overlay)
        legend_layout.setContentsMargins(7, 5, 7, 6)
        legend_layout.setSpacing(4)
        legend_header = self.QtWidgets.QHBoxLayout()
        legend_header.setContentsMargins(0, 0, 0, 0)
        legend_header.setSpacing(5)
        legend_title = self.QtWidgets.QLabel("Editor keys")
        legend_title.setObjectName("curveEditorShortcutTitle")
        legend_toggle = self.QtWidgets.QToolButton()
        legend_toggle.setObjectName("curveEditorShortcutToggle")
        legend_toggle.setAutoRaise(True)
        legend_header.addWidget(legend_title)
        legend_header.addStretch(1)
        legend_header.addWidget(legend_toggle)
        legend_layout.addLayout(legend_header)
        legend_body = self.QtWidgets.QWidget()
        legend_grid = self.QtWidgets.QGridLayout(legend_body)
        legend_grid.setContentsMargins(0, 0, 0, 0)
        legend_grid.setHorizontalSpacing(7)
        legend_grid.setVerticalSpacing(3)
        shortcut_rows = _curve_editor_shortcut_legend_rows()
        split_at = (len(shortcut_rows) + 1) // 2
        for index, (keys, text) in enumerate(shortcut_rows):
            grid_row = index % split_at
            grid_col = 0 if index < split_at else 2
            key_label = self.QtWidgets.QLabel(keys)
            key_label.setObjectName("curveEditorShortcutKey")
            text_label = self.QtWidgets.QLabel(text)
            text_label.setObjectName("curveEditorShortcutText")
            legend_grid.addWidget(key_label, grid_row, grid_col)
            legend_grid.addWidget(text_label, grid_row, grid_col + 1)
        legend_layout.addWidget(legend_body)
        legend_state = {"collapsed": False}

        def position_shortcut_legend() -> None:
            legend_overlay.adjustSize()
            size = legend_overlay.sizeHint()
            legend_overlay.resize(size)
            margin = 12
            x_pos = int(margin)
            y_pos = max(
                int(margin),
                int(plot.widget.height()) - int(size.height()) - int(margin),
            )
            legend_overlay.move(x_pos, y_pos)
            legend_overlay.raise_()

        def sync_shortcut_legend() -> None:
            collapsed = bool(legend_state["collapsed"])
            legend_body.setVisible(not collapsed)
            legend_title.setText("Keys" if collapsed else "Editor keys")
            legend_toggle.setText("+" if collapsed else "-")
            legend_toggle.setToolTip(
                "Show curve editor shortcuts"
                if collapsed
                else "Hide curve editor shortcuts"
            )
            position_shortcut_legend()

        def toggle_shortcut_legend() -> None:
            legend_state["collapsed"] = not bool(legend_state["collapsed"])
            sync_shortcut_legend()

        legend_toggle.clicked.connect(toggle_shortcut_legend)
        resize_event_type = getattr(
            getattr(self.QtCore.QEvent, "Type", self.QtCore.QEvent),
            "Resize",
            None,
        )

        class CurveEditorLegendFilter(self.QtCore.QObject):
            def eventFilter(filter_self, _watched, event) -> bool:
                if resize_event_type is not None and event.type() == resize_event_type:
                    position_shortcut_legend()
                return False

        legend_filter = CurveEditorLegendFilter(dialog)
        plot.widget.installEventFilter(legend_filter)
        sync_shortcut_legend()
        self.QtCore.QTimer.singleShot(0, position_shortcut_legend)
        dialog._curve_editor_shortcut_legend = legend_overlay
        dialog._curve_editor_shortcut_legend_filter = legend_filter

        status_label = self.QtWidgets.QLabel()
        status_label.setTextInteractionFlags(self.QtCore.Qt.TextSelectableByMouse)
        layout.addWidget(status_label)

        button_row = self.QtWidgets.QHBoxLayout()
        revert_button = self.QtWidgets.QPushButton("Revert")
        button_row.addWidget(revert_button)
        undo_button = self.QtWidgets.QPushButton("Undo")
        undo_button.setToolTip("Undo last edit (Ctrl+Z)")
        button_row.addWidget(undo_button)
        redo_button = self.QtWidgets.QPushButton("Redo")
        redo_button.setToolTip("Redo last undone edit (Ctrl+Y)")
        button_row.addWidget(redo_button)
        button_row.addStretch(1)
        save_button = self.QtWidgets.QPushButton("Save")
        standard_pixmap = getattr(
            self.QtWidgets.QStyle,
            "StandardPixmap",
            self.QtWidgets.QStyle,
        )

        def clone_edit(edit: ManualCurveEdit) -> ManualCurveEdit:
            return ManualCurveEdit(
                plan=[dict(item) for item in edit.plan],
                anchor_voltage_mv=int(edit.anchor_voltage_mv),
                anchor_clock_mhz=int(edit.anchor_clock_mhz),
                edit_kind=str(edit.edit_kind),
                selected_voltage_mv=edit.selected_voltage_mv,
                selected_clock_mhz=edit.selected_clock_mhz,
                selected_range_start_voltage_mv=edit.selected_range_start_voltage_mv,
            )

        def edit_signature(edit: ManualCurveEdit):
            return (
                int(edit.anchor_voltage_mv),
                int(edit.anchor_clock_mhz),
                str(edit.edit_kind),
                edit.selected_voltage_mv,
                edit.selected_clock_mhz,
                edit.selected_range_start_voltage_mv,
                tuple(
                    (
                        int(point.get("index", 0)),
                        int(point.get("voltage_mv", 0)),
                        int(point.get("base_mhz", 0)),
                        int(point.get("target_mhz", 0)),
                        int(point.get("new_offset_mhz", 0)),
                    )
                    for point in edit.plan
                ),
            )

        def edits_equal(left: ManualCurveEdit, right: ManualCurveEdit) -> bool:
            return edit_signature(left) == edit_signature(right)

        def update_history_buttons() -> None:
            undo_button.setEnabled(bool(undo_stack))
            redo_button.setEnabled(bool(redo_stack))

        def push_undo(edit: ManualCurveEdit, *, clear_redo: bool = True) -> None:
            if undo_stack and edits_equal(undo_stack[-1], edit):
                return
            undo_stack.append(clone_edit(edit))
            del undo_stack[:-64]
            if clear_redo:
                redo_stack.clear()
            update_history_buttons()

        def begin_undo_action(action_key: str, edit: ManualCurveEdit) -> None:
            if action_key in active_undo_actions:
                return
            snapshot = clone_edit(edit)
            active_undo_actions[action_key] = snapshot
            push_undo(snapshot)

        def finish_undo_action(action_key: str) -> None:
            snapshot = active_undo_actions.pop(action_key, None)
            if snapshot is None:
                return
            if edits_equal(current_edit["value"], snapshot) and undo_stack:
                undo_stack.pop()
            update_history_buttons()

        def undo_edit() -> None:
            if not undo_stack:
                return
            active_undo_actions.clear()
            previous = undo_stack.pop()
            redo_stack.append(clone_edit(current_edit["value"]))
            del redo_stack[:-64]
            set_preview(previous, move_target=True, record_undo=False)
            update_history_buttons()

        def redo_edit() -> None:
            if not redo_stack:
                return
            active_undo_actions.clear()
            next_edit = redo_stack.pop()
            undo_stack.append(clone_edit(current_edit["value"]))
            del undo_stack[:-64]
            set_preview(next_edit, move_target=True, record_undo=False)
            update_history_buttons()

        def nudge_selected_frequency(direction: int) -> None:
            active_undo_actions.clear()
            edit = manual_nudge_selected_frequency(
                current_edit["value"],
                direction=int(direction),
            )
            set_preview(edit, move_target=True, record_undo=True)

        def nudge_selected_voltage(direction: int) -> None:
            active_undo_actions.clear()
            edit = manual_nudge_selected_voltage(
                current_edit["value"],
                direction=int(direction),
            )
            set_preview(edit, move_target=True, record_undo=True)

        def select_adjacent_curve_point(direction: int) -> None:
            active_undo_actions.clear()
            edit = manual_select_adjacent_point(
                current_edit["value"],
                direction=int(direction),
            )
            set_preview(edit, move_target=True, record_undo=False)

        def select_range_to_right() -> None:
            active_undo_actions.clear()
            edit = manual_select_range_to_right(current_edit["value"])
            set_preview(edit, move_target=True, record_undo=False)

        def lock_selected_curve_point() -> None:
            active_undo_actions.clear()
            selected_voltage_mv = (
                current_edit["value"].selected_voltage_mv
                if current_edit["value"].selected_voltage_mv is not None
                else current_edit["value"].anchor_voltage_mv
            )
            edit = manual_flatten_from_existing_point(
                current_edit["value"].plan,
                anchor_voltage_mv=int(current_edit["value"].anchor_voltage_mv),
                clicked_voltage_mv=float(selected_voltage_mv),
            )
            set_preview(edit, record_undo=True)

        save_icon_id = getattr(standard_pixmap, "SP_DialogSaveButton", None)
        if save_icon_id is not None:
            save_button.setIcon(dialog.style().standardIcon(save_icon_id))
        cancel_button = self.QtWidgets.QPushButton("Cancel")
        button_row.addWidget(save_button)
        button_row.addWidget(cancel_button)
        layout.addLayout(button_row)

        def update_status(edit: ManualCurveEdit) -> None:
            selected_voltage_mv = (
                edit.selected_voltage_mv
                if edit.selected_voltage_mv is not None
                else edit.anchor_voltage_mv
            )
            selected_clock_mhz = (
                edit.selected_clock_mhz
                if edit.selected_clock_mhz is not None
                else edit.anchor_clock_mhz
            )
            if edit.selected_range_start_voltage_mv is not None:
                status_label.setText(
                    "Selected range: "
                    f"{int(edit.selected_range_start_voltage_mv)} mV -> flat tail. "
                    f"Start bin: {int(selected_voltage_mv)} mV / "
                    f"{int(selected_clock_mhz)} MHz. "
                    f"Flat max: {int(edit.anchor_clock_mhz)} MHz. "
                    "Up/Down or drag offsets the selected range."
                )
                return
            prefix = "Edited bin" if edit.edit_kind == "single-point" else "Selected bin"
            status_label.setText(
                f"{prefix}: "
                f"{int(selected_voltage_mv)} mV / "
                f"{int(selected_clock_mhz)} MHz. "
                f"Flat max: {int(edit.anchor_clock_mhz)} MHz. "
                "Saved edits require Verify before Apply."
            )

        def selected_range_points(edit: ManualCurveEdit) -> list[tuple[float, float]]:
            if edit.selected_range_start_voltage_mv is None:
                return []
            range_start_voltage_mv = int(edit.selected_range_start_voltage_mv)
            points = []
            for raw in edit.plan:
                try:
                    voltage_mv = int(raw["voltage_mv"])
                    clock_mhz = int(raw["target_mhz"])
                except (KeyError, TypeError, ValueError):
                    continue
                if voltage_mv < range_start_voltage_mv:
                    continue
                if voltage_mv >= int(edit.anchor_voltage_mv):
                    clock_mhz = int(edit.anchor_clock_mhz)
                points.append((float(voltage_mv), float(clock_mhz)))
            return sorted(points)

        def update_selected_range_overlay(edit: ManualCurveEdit) -> None:
            points = selected_range_points(edit)
            selected_range_overlay.setData(
                [point[0] for point in points],
                [point[1] for point in points],
            )

        def snap_target(edit: ManualCurveEdit) -> None:
            syncing_target["active"] = True
            try:
                target_item.setPos(
                    float(edit.anchor_voltage_mv),
                    float(edit.anchor_clock_mhz),
                )
            finally:
                syncing_target["active"] = False

        def set_preview(
            edit: ManualCurveEdit,
            *,
            move_target: bool = True,
            record_undo: bool = False,
        ) -> None:
            if record_undo and not edits_equal(current_edit["value"], edit):
                push_undo(current_edit["value"])
            current_edit["value"] = edit
            plot.set_candidate_points(
                _curve_points_from_values(edit.plan),
                remember_previous=False,
            )
            update_selected_range_overlay(edit)
            selected_voltage_mv = (
                edit.selected_voltage_mv
                if edit.selected_voltage_mv is not None
                else edit.anchor_voltage_mv
            )
            selected_clock_mhz = (
                edit.selected_clock_mhz
                if edit.selected_clock_mhz is not None
                else edit.anchor_clock_mhz
            )
            plot.set_selected_point(selected_voltage_mv, selected_clock_mhz)
            update_status(edit)
            if move_target:
                snap_target(edit)
            refresh_left_point_handles(edit)

        def target_position() -> tuple[float, float]:
            pos = target_item.pos()
            return float(pos.x()), float(pos.y())

        def on_target_position_changed(*_args) -> None:
            if syncing_target["active"]:
                return
            requested_voltage_mv, requested_clock_mhz = target_position()
            current_anchor = current_edit["value"]
            if current_anchor.selected_range_start_voltage_mv is not None:
                edit = manual_offset_selected_range(
                    current_anchor,
                    reference_voltage_mv=float(current_anchor.anchor_voltage_mv),
                    requested_clock_mhz=requested_clock_mhz,
                )
            else:
                edit = manual_drag_anchor_edit(
                    current_anchor.plan,
                    anchor_voltage_mv=int(current_anchor.anchor_voltage_mv),
                    anchor_clock_mhz=int(current_anchor.anchor_clock_mhz),
                    requested_voltage_mv=requested_voltage_mv,
                    requested_clock_mhz=requested_clock_mhz,
                )
            if not edits_equal(edit, current_anchor):
                begin_undo_action("target", current_anchor)
            set_preview(edit, move_target=True)

        def on_target_position_finished(*_args) -> None:
            finish_undo_action("target")
            snap_target(current_edit["value"])

        def revert_edit() -> None:
            active_undo_actions.clear()
            undo_stack.clear()
            redo_stack.clear()
            set_preview(initial_manual_edit())
            update_history_buttons()

        def left_point_values(edit: ManualCurveEdit) -> dict[int, int]:
            values = {}
            anchor_voltage_mv = int(edit.anchor_voltage_mv)
            for raw in edit.plan:
                try:
                    voltage_mv = int(raw["voltage_mv"])
                    clock_mhz = int(raw["target_mhz"])
                except (KeyError, TypeError, ValueError):
                    continue
                if voltage_mv < anchor_voltage_mv:
                    values[voltage_mv] = clock_mhz
            return dict(sorted(values.items()))

        def selectable_curve_points(edit: ManualCurveEdit) -> list[tuple[float, float]]:
            points = []
            anchor_voltage_mv = int(edit.anchor_voltage_mv)
            for raw in edit.plan:
                try:
                    voltage_mv = int(raw["voltage_mv"])
                    clock_mhz = int(raw["target_mhz"])
                except (KeyError, TypeError, ValueError):
                    continue
                if voltage_mv > anchor_voltage_mv:
                    continue
                if voltage_mv == anchor_voltage_mv:
                    clock_mhz = int(edit.anchor_clock_mhz)
                points.append((float(voltage_mv), float(clock_mhz)))
            return sorted(points)

        def select_curve_point(voltage_mv: float) -> None:
            active_undo_actions.clear()
            edit = manual_select_curve_point(
                current_edit["value"],
                voltage_mv=float(voltage_mv),
            )
            set_preview(edit, move_target=True, record_undo=False)

        def create_curve_point_at_scene_pos(scene_pos) -> bool:
            if not plot.plot.sceneBoundingRect().contains(scene_pos):
                return False
            view_pos = plot.plot.plotItem.vb.mapSceneToView(scene_pos)
            active_undo_actions.clear()
            edit = manual_tune_single_point_edit(
                current_edit["value"].plan,
                anchor_voltage_mv=int(current_edit["value"].anchor_voltage_mv),
                anchor_clock_mhz=int(current_edit["value"].anchor_clock_mhz),
                point_voltage_mv=float(view_pos.x()),
                requested_clock_mhz=float(view_pos.y()),
            )
            set_preview(edit, move_target=True, record_undo=True)
            return True

        target_mouse_click_event = target_item.mouseClickEvent

        def on_target_mouse_click_event(event) -> None:
            select_curve_point(float(current_edit["value"].anchor_voltage_mv))
            target_mouse_click_event(event)

        target_item.mouseClickEvent = on_target_mouse_click_event

        def left_point_item(entry):
            if isinstance(entry, dict):
                return entry.get("item")
            return entry

        def style_left_point_handle(item, *, selected: bool) -> None:
            if item is None:
                return
            if selected:
                item.setPen(self.pg.mkPen(self.pg.mkColor(255, 229, 177, 230), width=1))
                item.setBrush(self.pg.mkBrush(self.pg.mkColor(240, 184, 76, 225)))
            else:
                item.setPen(self.pg.mkPen(self.pg.mkColor(210, 226, 235, 190), width=1))
                item.setBrush(self.pg.mkBrush(self.pg.mkColor(94, 243, 140, 110)))

        def left_point_is_range_selected(edit: ManualCurveEdit, voltage_mv: int) -> bool:
            return bool(
                edit.selected_range_start_voltage_mv is not None
                and int(voltage_mv) >= int(edit.selected_range_start_voltage_mv)
            )

        def add_left_point_handle(voltage_mv: int, clock_mhz: int) -> None:
            item = self.pg.TargetItem(
                pos=(float(voltage_mv), float(clock_mhz)),
                size=8,
                symbol="o",
                pen=self.pg.mkPen(self.pg.mkColor(210, 226, 235, 190), width=1),
                brush=self.pg.mkBrush(self.pg.mkColor(94, 243, 140, 110)),
                movable=True,
            )
            if hasattr(item, "setZValue"):
                item.setZValue(6)

            item_mouse_click_event = item.mouseClickEvent

            def on_item_mouse_click_event(
                event,
                point_voltage_mv=int(voltage_mv),
                original_mouse_click=item_mouse_click_event,
            ) -> None:
                select_curve_point(float(point_voltage_mv))
                original_mouse_click(event)

            item.mouseClickEvent = on_item_mouse_click_event

            def on_changed(*_args, point_voltage_mv=int(voltage_mv)) -> None:
                on_left_point_position_changed(point_voltage_mv)

            def on_finished(*_args, point_voltage_mv=int(voltage_mv)) -> None:
                finish_undo_action(f"left-point:{int(point_voltage_mv)}")
                snap_left_point_handle(point_voltage_mv)

            item.sigPositionChanged.connect(on_changed)
            item.sigPositionChangeFinished.connect(on_finished)
            plot.plot.addItem(item)
            left_point_items[int(voltage_mv)] = {
                "item": item,
                "on_changed": on_changed,
                "on_finished": on_finished,
            }

        def refresh_left_point_handles(edit: ManualCurveEdit) -> None:
            values = left_point_values(edit)
            for voltage_mv in list(left_point_items):
                if voltage_mv not in values:
                    item = left_point_item(left_point_items.pop(voltage_mv))
                    if item is not None:
                        plot.plot.removeItem(item)
            for voltage_mv, clock_mhz in values.items():
                if voltage_mv not in left_point_items:
                    add_left_point_handle(voltage_mv, clock_mhz)
            syncing_left_points["active"] = True
            try:
                for voltage_mv, clock_mhz in values.items():
                    item = left_point_item(left_point_items.get(voltage_mv))
                    if item is not None:
                        item.setPos(float(voltage_mv), float(clock_mhz))
                        style_left_point_handle(
                            item,
                            selected=left_point_is_range_selected(edit, voltage_mv),
                        )
            finally:
                syncing_left_points["active"] = False

        def snap_left_point_handle(voltage_mv: int) -> None:
            values = left_point_values(current_edit["value"])
            item = left_point_item(left_point_items.get(int(voltage_mv)))
            clock_mhz = values.get(int(voltage_mv))
            if item is None or clock_mhz is None:
                return
            syncing_left_points["active"] = True
            try:
                item.setPos(float(voltage_mv), float(clock_mhz))
            finally:
                syncing_left_points["active"] = False

        def on_left_point_position_changed(voltage_mv: int) -> None:
            if syncing_left_points["active"]:
                return
            item = left_point_item(left_point_items.get(int(voltage_mv)))
            if item is None:
                return
            pos = item.pos()
            current_value = current_edit["value"]
            range_start_voltage_mv = current_value.selected_range_start_voltage_mv
            if (
                range_start_voltage_mv is not None
                and int(voltage_mv) >= int(range_start_voltage_mv)
            ):
                edit = manual_offset_selected_range(
                    current_value,
                    reference_voltage_mv=int(voltage_mv),
                    requested_clock_mhz=float(pos.y()),
                )
            else:
                edit = manual_tune_single_point_edit(
                    current_value.plan,
                    anchor_voltage_mv=int(current_value.anchor_voltage_mv),
                    anchor_clock_mhz=int(current_value.anchor_clock_mhz),
                    point_voltage_mv=int(voltage_mv),
                    requested_voltage_mv=float(pos.x()),
                    requested_clock_mhz=float(pos.y()),
                )
            if not edits_equal(edit, current_edit["value"]):
                begin_undo_action(
                    f"left-point:{int(voltage_mv)}",
                    current_edit["value"],
                )
            set_preview(edit, move_target=False)

        def nearest_left_point_at_scene_pos(scene_pos):
            if not plot.plot.sceneBoundingRect().contains(scene_pos):
                return None
            view_pos = plot.plot.plotItem.vb.mapSceneToView(scene_pos)
            left_points = [
                (float(voltage_mv), float(clock_mhz))
                for voltage_mv, clock_mhz in left_point_values(
                    current_edit["value"]
                ).items()
            ]
            return _nearest_profile_curve_point(
                float(view_pos.x()),
                float(view_pos.y()),
                left_points,
                plot.plot.viewRange(),
                max_normalized_distance=0.08,
            )

        def select_nearest_curve_point_at_scene_pos(scene_pos) -> bool:
            if not plot.plot.sceneBoundingRect().contains(scene_pos):
                return False
            view_pos = plot.plot.plotItem.vb.mapSceneToView(scene_pos)
            point = _nearest_profile_curve_point(
                float(view_pos.x()),
                float(view_pos.y()),
                selectable_curve_points(current_edit["value"]),
                plot.plot.viewRange(),
                max_normalized_distance=0.08,
            )
            if point is None:
                return False
            select_curve_point(point[0])
            return True

        def event_global_pos(event, scene_pos):
            screen_pos_attr = getattr(event, "screenPos", None)
            if callable(screen_pos_attr):
                screen_pos = screen_pos_attr()
                try:
                    return self.QtCore.QPoint(
                        int(round(float(screen_pos.x()))),
                        int(round(float(screen_pos.y()))),
                    )
                except (TypeError, ValueError, AttributeError):
                    pass
            try:
                widget_pos = plot.plot.mapFromScene(scene_pos)
                return plot.plot.mapToGlobal(widget_pos)
            except (TypeError, AttributeError):
                return self.QtGui.QCursor.pos()

        def show_left_point_context_menu(event, scene_pos) -> bool:
            point = nearest_left_point_at_scene_pos(scene_pos)
            if point is None:
                return False
            menu = self.QtWidgets.QMenu(dialog)
            flatten_action = menu.addAction("Flatten at This Point")
            chosen = menu.exec(event_global_pos(event, scene_pos))
            if chosen == flatten_action:
                edit = manual_flatten_from_existing_point(
                    current_edit["value"].plan,
                    anchor_voltage_mv=int(current_edit["value"].anchor_voltage_mv),
                    clicked_voltage_mv=float(point[0]),
                )
                set_preview(edit, record_undo=True)
            return True

        def event_is_right_click(event) -> bool:
            button_attr = getattr(event, "button", None)
            button = button_attr() if callable(button_attr) else button_attr
            right_button = getattr(self.QtCore.Qt, "RightButton", None)
            mouse_button = getattr(self.QtCore.Qt, "MouseButton", None)
            if mouse_button is not None:
                right_button = getattr(mouse_button, "RightButton", right_button)
            return bool(right_button is not None and button == right_button)

        def event_has_keyboard_modifier(event, modifier_name: str) -> bool:
            modifier_enum = getattr(
                self.QtCore.Qt,
                "KeyboardModifier",
                self.QtCore.Qt,
            )
            modifier = getattr(modifier_enum, str(modifier_name), None)
            modifiers_attr = getattr(event, "modifiers", None)
            if modifier is None or not callable(modifiers_attr):
                return False
            return bool(modifiers_attr() & modifier)

        def on_plot_clicked(event) -> None:
            if event_is_right_click(event):
                if show_left_point_context_menu(event, event.scenePos()):
                    if hasattr(event, "accept"):
                        event.accept()
                return
            if event_has_keyboard_modifier(event, "ControlModifier"):
                if not create_curve_point_at_scene_pos(event.scenePos()):
                    return
                if hasattr(event, "accept"):
                    event.accept()
                return
            if not select_nearest_curve_point_at_scene_pos(event.scenePos()):
                return
            if hasattr(event, "accept"):
                event.accept()

        context_menu_event_types = {
            value
            for value in (
                getattr(
                    getattr(self.QtCore.QEvent, "Type", self.QtCore.QEvent),
                    "GraphicsSceneContextMenu",
                    None,
                ),
                getattr(
                    getattr(self.QtCore.QEvent, "Type", self.QtCore.QEvent),
                    "ContextMenu",
                    None,
                ),
            )
            if value is not None
        }

        class CurveEditorContextMenuFilter(self.QtCore.QObject):
            def eventFilter(filter_self, _watched, event) -> bool:
                if event.type() in context_menu_event_types:
                    scene_pos = (
                        event.scenePos()
                        if hasattr(event, "scenePos")
                        else plot.plot.mapToScene(event.pos())
                    )
                    if not show_left_point_context_menu(event, scene_pos):
                        return False
                    if hasattr(event, "accept"):
                        event.accept()
                    return True
                return False

        key_press_event_type = getattr(
            getattr(self.QtCore.QEvent, "Type", self.QtCore.QEvent),
            "KeyPress",
            None,
        )
        key_enum = getattr(self.QtCore.Qt, "Key", self.QtCore.Qt)
        modifier_enum = getattr(
            self.QtCore.Qt,
            "KeyboardModifier",
            self.QtCore.Qt,
        )
        key_left = getattr(key_enum, "Key_Left", None)
        key_right = getattr(key_enum, "Key_Right", None)
        key_up = getattr(key_enum, "Key_Up", None)
        key_down = getattr(key_enum, "Key_Down", None)
        key_tab = getattr(key_enum, "Key_Tab", None)
        key_backtab = getattr(key_enum, "Key_Backtab", None)
        key_l = getattr(key_enum, "Key_L", None)
        key_z = getattr(key_enum, "Key_Z", None)
        key_y = getattr(key_enum, "Key_Y", None)
        control_modifier = getattr(modifier_enum, "ControlModifier", None)
        shift_modifier = getattr(modifier_enum, "ShiftModifier", None)
        alt_modifier = getattr(modifier_enum, "AltModifier", None)

        def event_has_modifier(event, modifier) -> bool:
            if modifier is None:
                return False
            return bool(event.modifiers() & modifier)

        def key_event_belongs_to_dialog() -> bool:
            focus_widget = self.QtWidgets.QApplication.focusWidget()
            if focus_widget is None:
                return bool(dialog.isActiveWindow())
            return bool(focus_widget is dialog or dialog.isAncestorOf(focus_widget))

        class CurveEditorKeyFilter(self.QtCore.QObject):
            def eventFilter(filter_self, _watched, event) -> bool:
                if key_press_event_type is None or event.type() != key_press_event_type:
                    return False
                if not dialog.isVisible() or not key_event_belongs_to_dialog():
                    return False
                key = event.key()
                has_ctrl = event_has_modifier(event, control_modifier)
                has_shift = event_has_modifier(event, shift_modifier)
                has_alt = event_has_modifier(event, alt_modifier)
                if has_ctrl and not has_alt and key == key_l:
                    lock_selected_curve_point()
                elif has_ctrl and not has_alt and key == key_z:
                    undo_edit()
                elif has_ctrl and not has_alt and key == key_y:
                    redo_edit()
                elif not has_ctrl and has_shift and not has_alt and key == key_right:
                    select_range_to_right()
                elif not has_ctrl and not has_shift and not has_alt and key == key_up:
                    nudge_selected_frequency(1)
                elif not has_ctrl and not has_shift and not has_alt and key == key_down:
                    nudge_selected_frequency(-1)
                elif not has_ctrl and not has_shift and not has_alt and key == key_left:
                    nudge_selected_voltage(-1)
                elif not has_ctrl and not has_shift and not has_alt and key == key_right:
                    nudge_selected_voltage(1)
                elif not has_ctrl and not has_alt and key == key_tab:
                    select_adjacent_curve_point(-1 if has_shift else 1)
                elif not has_ctrl and not has_alt and key == key_backtab:
                    select_adjacent_curve_point(-1)
                else:
                    return False
                if hasattr(event, "accept"):
                    event.accept()
                return True

        def save_edit() -> None:
            edit = current_edit["value"]
            payload = user_edited_profile_payload(
                parent_payload,
                edit,
                original_anchor_voltage_mv=int(original_anchor_voltage_mv),
                original_anchor_clock_mhz=int(original_anchor_clock_mhz),
            )
            try:
                path = archive_auto_uv_profile(payload)
            except Exception as exc:
                self.QtWidgets.QMessageBox.critical(
                    dialog,
                    "Edit VF Curve",
                    f"Failed to save edited curve: {exc}",
                )
                return
            saved_profile_id = _profile_id_from_archive_path(path)
            if saved_profile_id:
                self.last_auto_uv_profile_id = saved_profile_id
            self.last_auto_uv_candidate_id = str(payload.get("candidate_id", "")).strip()
            self.controls.set_status_text(
                f"Saved edited curve draft: {path.name}. Verify it before Apply."
            )
            self._load_profiles(prefer_last_auto_uv=True, select_last_auto_uv=True)
            if saved_profile_id:
                self.profile_list.select_profile(saved_profile_id)
                self._update_runner_status()
            dialog.accept()

        target_item.sigPositionChanged.connect(on_target_position_changed)
        target_item.sigPositionChangeFinished.connect(on_target_position_finished)
        plot.plot.scene().sigMouseClicked.connect(on_plot_clicked)
        context_menu_filter = CurveEditorContextMenuFilter(dialog)
        plot.plot.scene().installEventFilter(context_menu_filter)
        dialog._curve_editor_context_menu_filter = context_menu_filter
        refresh_left_point_handles(current_edit["value"])
        revert_button.clicked.connect(revert_edit)
        undo_button.clicked.connect(undo_edit)
        redo_button.clicked.connect(redo_edit)
        undo_shortcut = self.QtGui.QShortcut(
            self.QtGui.QKeySequence("Ctrl+Z"),
            dialog,
        )
        shortcut_context = getattr(
            getattr(self.QtCore.Qt, "ShortcutContext", self.QtCore.Qt),
            "WidgetWithChildrenShortcut",
            None,
        )
        if shortcut_context is not None:
            undo_shortcut.setContext(shortcut_context)
        undo_shortcut.activated.connect(undo_edit)
        dialog._curve_editor_undo_shortcut = undo_shortcut
        redo_shortcut = self.QtGui.QShortcut(
            self.QtGui.QKeySequence("Ctrl+Y"),
            dialog,
        )
        nudge_up_shortcut = self.QtGui.QShortcut(
            self.QtGui.QKeySequence("Up"),
            dialog,
        )
        nudge_down_shortcut = self.QtGui.QShortcut(
            self.QtGui.QKeySequence("Down"),
            dialog,
        )
        nudge_left_shortcut = self.QtGui.QShortcut(
            self.QtGui.QKeySequence("Left"),
            dialog,
        )
        nudge_right_shortcut = self.QtGui.QShortcut(
            self.QtGui.QKeySequence("Right"),
            dialog,
        )
        select_next_shortcut = self.QtGui.QShortcut(
            self.QtGui.QKeySequence("Tab"),
            dialog,
        )
        select_previous_shortcut = self.QtGui.QShortcut(
            self.QtGui.QKeySequence("Shift+Tab"),
            dialog,
        )
        lock_selected_shortcut = self.QtGui.QShortcut(
            self.QtGui.QKeySequence("Ctrl+L"),
            dialog,
        )
        select_range_shortcut = self.QtGui.QShortcut(
            self.QtGui.QKeySequence("Shift+Right"),
            dialog,
        )
        if shortcut_context is not None:
            redo_shortcut.setContext(shortcut_context)
            nudge_up_shortcut.setContext(shortcut_context)
            nudge_down_shortcut.setContext(shortcut_context)
            nudge_left_shortcut.setContext(shortcut_context)
            nudge_right_shortcut.setContext(shortcut_context)
            select_next_shortcut.setContext(shortcut_context)
            select_previous_shortcut.setContext(shortcut_context)
            lock_selected_shortcut.setContext(shortcut_context)
            select_range_shortcut.setContext(shortcut_context)
        redo_shortcut.activated.connect(redo_edit)
        nudge_up_shortcut.activated.connect(lambda: nudge_selected_frequency(1))
        nudge_down_shortcut.activated.connect(lambda: nudge_selected_frequency(-1))
        nudge_left_shortcut.activated.connect(lambda: nudge_selected_voltage(-1))
        nudge_right_shortcut.activated.connect(lambda: nudge_selected_voltage(1))
        select_next_shortcut.activated.connect(lambda: select_adjacent_curve_point(1))
        select_previous_shortcut.activated.connect(
            lambda: select_adjacent_curve_point(-1)
        )
        lock_selected_shortcut.activated.connect(lock_selected_curve_point)
        select_range_shortcut.activated.connect(select_range_to_right)
        dialog._curve_editor_redo_shortcut = redo_shortcut
        dialog._curve_editor_nudge_up_shortcut = nudge_up_shortcut
        dialog._curve_editor_nudge_down_shortcut = nudge_down_shortcut
        dialog._curve_editor_nudge_left_shortcut = nudge_left_shortcut
        dialog._curve_editor_nudge_right_shortcut = nudge_right_shortcut
        dialog._curve_editor_select_next_shortcut = select_next_shortcut
        dialog._curve_editor_select_previous_shortcut = select_previous_shortcut
        dialog._curve_editor_lock_selected_shortcut = lock_selected_shortcut
        dialog._curve_editor_select_range_shortcut = select_range_shortcut
        save_button.clicked.connect(save_edit)
        cancel_button.clicked.connect(dialog.reject)
        update_status(current_edit["value"])
        update_history_buttons()
        app_instance = self.QtWidgets.QApplication.instance()
        key_filter = CurveEditorKeyFilter(dialog)
        if app_instance is not None:
            app_instance.installEventFilter(key_filter)
        dialog._curve_editor_key_filter = key_filter
        try:
            dialog.exec()
        finally:
            if app_instance is not None:
                app_instance.removeEventFilter(key_filter)

    def _open_profile_fan_curve_tab(self, profile: dict) -> None:
        self._open_profile_fan_curve_editor(profile)

    def _open_profile_fan_curve_editor(self, profile: dict) -> None:
        if self.pg is None:
            self.controls.set_status_text("pyqtgraph is required to edit fan curves.")
            self.QtWidgets.QMessageBox.information(
                self.window,
                "Edit Fan Curve",
                "pyqtgraph is not installed, so the fan curve editor is unavailable.",
            )
            return
        curve_points = _profile_fan_curve_points(profile)
        if not curve_points:
            self.controls.set_status_text("No fan curve is available for this profile.")
            self.QtWidgets.QMessageBox.information(
                self.window,
                "Edit Fan Curve",
                "No fan curve is available for this profile.",
            )
            return
        fan_payload = _profile_fan_payload(profile) or {}
        parent_payload = _profile_payload_from_path(profile) or dict(profile)
        measured_points = _profile_fan_measurement_points(profile)
        target = _profile_fan_curve_target_point(profile)

        def save_edit(edit) -> str:
            if not _profile_payload_has_saved_vf_curve(parent_payload):
                raise ValueError(
                    "selected profile does not contain a saved Auto-UV V/F curve"
                )
            payload = user_edited_fan_curve_profile_payload(
                parent_payload,
                fan_payload,
                edit,
                original_points=curve_points,
            )
            fan_curve_payload = payload.get("fan_curve_payload")
            path = archive_auto_uv_profile(payload)
            if isinstance(fan_curve_payload, dict):
                _write_auto_uv_fan_curve_payload(fan_curve_payload)
            saved_profile_id = _profile_id_from_archive_path(path)
            if saved_profile_id:
                self.last_auto_uv_profile_id = saved_profile_id
            self.last_auto_uv_candidate_id = str(payload.get("candidate_id", "")).strip()
            self.controls.set_status_text(
                f"Saved edited fan curve: {path.name}."
            )
            if isinstance(fan_curve_payload, dict):
                self._populate_fan_plot_from_payload(fan_curve_payload)
            self._load_profiles(prefer_last_auto_uv=True, select_last_auto_uv=True)
            if saved_profile_id:
                self.profile_list.select_profile(saved_profile_id)
                self._update_runner_status()
            return f"Saved edited fan curve: {path.name}."

        open_fan_curve_editor_dialog(
            QtCore=self.QtCore,
            QtGui=self.QtGui,
            QtWidgets=self.QtWidgets,
            pg=self.pg,
            parent=self.window,
            curve_points=curve_points,
            measured_points=measured_points,
            target_point=target,
            save_callback=save_edit,
        )

    def _apply_profile(self, profile: dict) -> None:
        if not self._profile_actions_available():
            return
        if not _profile_can_apply(profile):
            self.controls.set_status_text(
                "This edited profile must be re-verified before it can be applied."
            )
            self.QtWidgets.QMessageBox.information(
                self.window,
                "Apply Profile",
                "This edited profile must be re-verified before it can be applied.",
            )
            return
        profile_id = str(profile.get("profile_id", "")).strip()
        if not profile_id:
            return
        self.profile_list.select_profile(profile_id)
        self.profile_list.install_button.setChecked(True)
        self._run_runtime_action("install-systemd")

    def _reverify_profile(self, profile: dict) -> None:
        if not self._profile_actions_available():
            return
        reverify_options = self._select_reverify_duration_dialog(profile)
        if reverify_options is None:
            return
        duration_s = int(reverify_options["duration_s"])
        q2rtx_enabled = bool(reverify_options["q2rtx_enabled"])
        cuda_enabled = bool(reverify_options["cuda_enabled"])
        profile_id = str(profile.get("profile_id", "")).strip()
        profile_selector = _profile_verify_selector(profile)
        prefer_afterburner_curve = _profile_is_afterburner(profile)
        if profile_id:
            self.profile_list.select_profile(profile_id)
        stop_request_path = _reverify_stop_request_path()
        stop_request_path.parent.mkdir(parents=True, exist_ok=True)
        stop_request_path.unlink(missing_ok=True)
        command = profile_reverify_command(
            profile_selector="" if prefer_afterburner_curve else profile_selector,
            duration_s=int(duration_s),
            prefer_afterburner_curve=prefer_afterburner_curve,
            stop_request_path=stop_request_path,
            q2rtx_enabled=q2rtx_enabled,
            cuda_enabled=cuda_enabled,
        )
        label = _profile_status_label([profile], profile_id) or "selected profile"
        duration_label = _format_duration_for_user(duration_s)
        workload_label = _reverify_workload_label(
            q2rtx_enabled=q2rtx_enabled,
            cuda_enabled=cuda_enabled,
        )
        self.header.set_stage("Profile verification")
        self.header.set_candidate(label)
        self.controls.set_status_text(
            f"Verifying {label} with {workload_label} for {duration_label}."
        )
        self.tabs.setCurrentIndex(self.auto_uv_tab_index)
        self.log_view.text_edit.setFocus()
        self.reverify_target_duration_s = int(duration_s)
        self.reverify_last_elapsed_s = 0.0
        self.reverify_stop_requested = False
        self.controls.set_reverify_progress(
            0,
            elapsed_s=0,
            target_s=int(duration_s),
            detail=f"Verifying {label} with {workload_label} for {duration_label}.",
        )
        self.log_view.append(
            "\n$ " + " ".join(shlex.quote(part) for part in command) + "\n"
        )
        process = self.QtCore.QProcess(self.window)
        process.setProcessChannelMode(self.QtCore.QProcess.MergedChannels)
        process.readyReadStandardOutput.connect(self._read_runtime_process_output)
        process.finished.connect(self._runtime_process_finished)
        self.runtime_process = process
        self.runtime_process_kind = "reverify"
        self.active_reverify_stop_request_path = stop_request_path
        self.profile_list.set_runtime_actions_enabled(False)
        self.controls.set_running(True)
        process.start(command[0], command[1:])
        if not process.waitForStarted(3000):
            self.log_view.append("Failed to start profile verification.\n")
            self._runtime_process_finished(-1, self.QtCore.QProcess.CrashExit)

    def _select_reverify_duration_dialog(self, profile: dict) -> dict | None:
        dialog = self.QtWidgets.QDialog(self.window)
        dialog.setWindowTitle("Verify Profile")
        layout = self.QtWidgets.QVBoxLayout(dialog)
        label = _profile_status_label(
            [profile],
            str(profile.get("profile_id", "")).strip(),
        )
        intro = self.QtWidgets.QLabel(
            f"Verify {label or 'the selected profile'} with the selected workload."
        )
        intro.setWordWrap(True)
        q2rtx_checkbox = self.QtWidgets.QCheckBox("Q2RTX timedemo")
        q2rtx_checkbox.setChecked(True)
        cuda_checkbox = self.QtWidgets.QCheckBox("CUDA compute test")
        cuda_checkbox.setChecked(True)
        syncing_workloads = False

        def keep_one_workload_checked(changed_checkbox) -> None:
            nonlocal syncing_workloads
            if syncing_workloads:
                return
            if q2rtx_checkbox.isChecked() or cuda_checkbox.isChecked():
                return
            syncing_workloads = True
            try:
                changed_checkbox.setChecked(True)
            finally:
                syncing_workloads = False

        q2rtx_checkbox.toggled.connect(
            lambda _checked: keep_one_workload_checked(q2rtx_checkbox)
        )
        cuda_checkbox.toggled.connect(
            lambda _checked: keep_one_workload_checked(cuda_checkbox)
        )
        duration_spin = self.QtWidgets.QSpinBox()
        duration_spin.setRange(1, MAX_FINAL_VERIFICATION_DURATION_S // 60)
        duration_spin.setSuffix(" min")
        duration_spin.setSingleStep(1)
        duration_spin.setValue(
            _duration_minutes_for_control(DEFAULT_FINAL_VERIFICATION_DURATION_S)
        )
        duration_layout = self.QtWidgets.QHBoxLayout()
        duration_layout.addWidget(self.QtWidgets.QLabel("Verification duration"))
        duration_layout.addWidget(duration_spin)
        duration_layout.addStretch(1)
        workload_layout = self.QtWidgets.QHBoxLayout()
        workload_layout.addWidget(self.QtWidgets.QLabel("Workloads"))
        workload_layout.addWidget(q2rtx_checkbox)
        workload_layout.addWidget(cuda_checkbox)
        workload_layout.addStretch(1)
        buttons = self.QtWidgets.QDialogButtonBox()
        start_button = buttons.addButton(
            "Start Verification",
            self.QtWidgets.QDialogButtonBox.AcceptRole,
        )
        buttons.addButton(self.QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        start_button.setDefault(True)
        layout.addWidget(intro)
        layout.addLayout(workload_layout)
        layout.addLayout(duration_layout)
        layout.addWidget(buttons)
        dialog.setMinimumWidth(420)
        if dialog.exec() != self.QtWidgets.QDialog.Accepted:
            return None
        return {
            "duration_s": _duration_seconds_from_minutes(duration_spin.value()),
            "q2rtx_enabled": q2rtx_checkbox.isChecked(),
            "cuda_enabled": cuda_checkbox.isChecked(),
        }

    def _export_lact_profile(self, profile: dict) -> None:
        if not self._profile_actions_available():
            return
        directory = self.QtWidgets.QFileDialog.getExistingDirectory(
            self.window,
            "Choose LACT Export Directory",
            str(default_user_config_dir()),
        )
        if not directory:
            return
        output_path = _lact_export_output_path(directory)
        gpu_id = _detect_lact_gpu_id(output_path.parent)
        if not gpu_id:
            message = (
                "Could not detect the LACT GPU id. Start LACT once, or choose a "
                f"directory that already contains {LACT_CONFIG_FILENAME}."
            )
            self.controls.set_status_text("LACT export failed.")
            self.log_view.append(f"\nLACT export failed: {message}\n")
            self._show_error_dialog(
                "Export LACT",
                message,
            )
            return
        try:
            written_path, warnings = _write_lact_profile_config(
                profile,
                output_path=output_path,
                gpu_id=gpu_id,
                include_fan_curve=self.profile_list.silent_fan_enabled(),
            )
        except Exception as exc:
            self.controls.set_status_text("LACT export failed.")
            self.log_view.append(f"\nLACT export failed: {exc}\n")
            self._show_error_dialog(
                "Export LACT",
                f"LACT export failed:\n{exc}",
            )
            return
        message = f"LACT profile successfully written:\n{written_path}"
        if warnings:
            message += "\n\nWarnings:\n" + "\n".join(str(item) for item in warnings)
        self.controls.set_status_text(f"LACT profile written: {written_path}")
        self.log_view.append("\n" + message + "\n")
        self.QtWidgets.QMessageBox.information(
            self.window,
            "Export LACT",
            message,
        )

    def _profile_actions_available(self) -> bool:
        return (
            self.process is None
            and self.runtime_process is None
            and self.profile_delete_process is None
        )

    def _run_selected_profile(self) -> None:
        action = (
            "install-systemd"
            if self.profile_list.persist_on_startup_enabled()
            else "daemonize"
        )
        self._run_runtime_action(action)

    def _run_runtime_action(self, action: str) -> None:
        if (
            self.runtime_process is not None
            or self.process is not None
            or self.profile_delete_process is not None
        ):
            return
        profile_id = self.profile_list.selected_profile_id()
        if action != "uninstall-systemd" and not profile_id:
            self.log_view.append("\nNo profile selected.\n")
            return
        selected_profile = _profile_for_selector(self.profile_summaries, profile_id)
        if action != "uninstall-systemd" and not _profile_can_apply(
            selected_profile or {}
        ):
            self.log_view.append(
                "\nThis edited profile must be re-verified before it can be applied.\n"
            )
            self.controls.set_status_text(
                "This edited profile must be re-verified before it can be applied."
            )
            return
        prefer_afterburner_curve = bool(
            selected_profile
            and str(selected_profile.get("runtime_source", "")) == "afterburner"
        )
        if (
            action != "uninstall-systemd"
            and selected_profile
            and self.profile_list.silent_fan_enabled()
        ):
            try:
                self._sync_selected_profile_fan_curve_payload(selected_profile)
            except Exception as exc:
                self.log_view.append(f"\nFailed to prepare selected fan curve: {exc}\n")
                self.controls.set_status_text("Failed to prepare selected fan curve.")
                return
        self.controls.set_status_text(self._runtime_action_start_text(action))
        command = runtime_profile_command(
            action,
            profile_selector="" if prefer_afterburner_curve else profile_id,
            silent_fan_curve=self.profile_list.silent_fan_enabled(),
            prefer_afterburner_curve=prefer_afterburner_curve,
        )
        self.log_view.append("\n$ " + " ".join(command) + "\n")
        process = self.QtCore.QProcess(self.window)
        process.setProcessChannelMode(self.QtCore.QProcess.MergedChannels)
        process.readyReadStandardOutput.connect(self._read_runtime_process_output)
        process.finished.connect(self._runtime_process_finished)
        self.runtime_process = process
        self.runtime_process_kind = action
        self.profile_list.set_runtime_actions_enabled(False)
        self.controls.start_button.setEnabled(False)
        process.start(command[0], command[1:])
        if not process.waitForStarted(3000):
            self.log_view.append("Failed to start runtime profile action.\n")
            self._runtime_process_finished(-1, self.QtCore.QProcess.CrashExit)

    def _read_runtime_process_output(self) -> None:
        if self.runtime_process is None:
            return
        data = bytes(self.runtime_process.readAllStandardOutput()).decode(
            "utf-8",
            errors="replace",
        )
        self.log_view.append(data)
        if self.runtime_process_kind == "reverify":
            self._handle_reverify_output(data)

    def _handle_reverify_output(self, data: str) -> None:
        target = int(self.reverify_target_duration_s or 0)
        if target <= 0:
            return
        for line in str(data or "").splitlines():
            elapsed = _reverify_elapsed_from_line(line)
            if elapsed is None:
                continue
            self.reverify_last_elapsed_s = max(
                float(self.reverify_last_elapsed_s),
                float(elapsed),
            )
            percent = _reverify_progress_percent(
                self.reverify_last_elapsed_s,
                target,
            )
            self.controls.set_reverify_progress(
                percent,
                elapsed_s=self.reverify_last_elapsed_s,
                target_s=target,
                detail=line.strip(),
            )

    def _runtime_process_finished(self, exit_code, exit_status) -> None:
        process_kind = self.runtime_process_kind
        label = (
            "Profile verification"
            if process_kind == "reverify"
            else _runtime_action_dialog_label(process_kind)
        )
        self.log_view.append(f"\n{label} finished: exit_code={exit_code}\n")
        stopped_by_user = (
            process_kind == "reverify" and bool(self.reverify_stop_requested)
        )
        if self.runtime_process is not None:
            self.runtime_process.deleteLater()
        self.runtime_process = None
        self.runtime_process_kind = ""
        self.profile_list.set_runtime_actions_enabled(self.process is None)
        if process_kind == "reverify":
            if int(exit_code) == 0:
                target = int(self.reverify_target_duration_s or 0)
                self.controls.set_reverify_progress(
                    100,
                    elapsed_s=target,
                    target_s=target,
                    detail="Profile verification complete.",
                )
                self.QtCore.QTimer.singleShot(
                    2500,
                    self.controls.hide_dependency_progress,
                )
            else:
                self.controls.hide_dependency_progress()
            self.reverify_target_duration_s = 0
            self.reverify_last_elapsed_s = 0.0
            self.reverify_stop_requested = False
            if self.active_reverify_stop_request_path is not None:
                self.active_reverify_stop_request_path.unlink(missing_ok=True)
            self.active_reverify_stop_request_path = None
            self.controls.set_running(False)
            self.header.set_stage(
                "Idle" if int(exit_code) == 0 or stopped_by_user else "Error"
            )
            self.controls.set_status_text(
                "Profile verification complete."
                if int(exit_code) == 0
                else (
                    "Profile verification stopped."
                    if stopped_by_user
                    else "Profile verification failed."
                )
            )
            if int(exit_code) != 0 and not stopped_by_user:
                self._show_process_error_dialog(
                    title="Profile verification failed",
                    action_label=label,
                    exit_code=exit_code,
                    exit_status=exit_status,
                )
        else:
            self.controls.start_button.setEnabled(self.process is None)
            if int(exit_code) != 0:
                self.controls.set_status_text(f"{label} failed.")
                self._show_process_error_dialog(
                    title=f"{label} failed",
                    action_label=label,
                    exit_code=exit_code,
                    exit_status=exit_status,
                )
        self._load_profiles(preserve_persist_toggle=False)

    def _delete_selected_profiles(self) -> None:
        if (
            self.process is not None
            or self.runtime_process is not None
            or self.profile_delete_process is not None
        ):
            return
        selected_ids = set(self.profile_list.selected_profile_ids())
        selected_profiles = _profiles_for_selectors(
            self.profile_summaries,
            list(selected_ids),
        )
        delete_afterburner_import = any(
            _profile_is_afterburner(profile) for profile in selected_profiles
        )
        selected_paths = self.profile_list.selected_profile_paths()
        if not selected_paths and not delete_afterburner_import:
            return
        remove_systemd = _selected_profile_ids_include_selector(
            self.profile_summaries,
            list(selected_ids),
            _systemd_autostart_profile_selector(),
        )
        if not self._confirm_profile_delete(
            self.profile_list.selected_profile_names(),
            removes_systemd=remove_systemd,
            includes_afterburner=delete_afterburner_import,
        ):
            return
        afterburner_deleted = False
        if delete_afterburner_import:
            try:
                afterburner_deleted = _delete_afterburner_import_config()
            except Exception as exc:
                self.controls.set_status_text("Afterburner import deletion failed.")
                self.log_view.append(f"\nAfterburner import deletion failed: {exc}\n")
                self._show_error_dialog(
                    "Delete Profiles",
                    f"Afterburner import deletion failed:\n{exc}",
                )
                return
        try:
            deleted = (
                delete_auto_uv_profile_paths(selected_paths) if selected_paths else []
            )
        except PermissionError:
            self._run_privileged_profile_delete(
                selected_paths,
                selected_ids,
                remove_systemd=remove_systemd,
            )
            return
        if deleted or afterburner_deleted:
            count = len(deleted) + (1 if afterburner_deleted else 0)
            label = "profile" if count == 1 else "profiles"
            self.log_view.append(f"\nDeleted {count} saved {label}.\n")
            self.controls.set_status_text(f"Deleted {count} saved {label}.")
            if self.last_auto_uv_profile_id in selected_ids:
                self.last_auto_uv_profile_id = ""
                self.last_auto_uv_candidate_id = ""
            self._load_profiles()
            if remove_systemd:
                self.log_view.append(
                    "Removing Systemd autostart entry for deleted profile.\n"
                )
                self._run_runtime_action("uninstall-systemd")
            return
        if not selected_paths:
            self._load_profiles()
            return
        self._run_privileged_profile_delete(
            selected_paths,
            selected_ids,
            remove_systemd=remove_systemd,
        )

    def _run_privileged_profile_delete(
        self,
        selected_paths: list[str],
        selected_ids: set[str],
        *,
        remove_systemd: bool = False,
    ) -> None:
        command = delete_profiles_command(selected_paths)
        self.log_view.append(
            "\n$ " + " ".join(shlex.quote(part) for part in command) + "\n"
        )
        process = self.QtCore.QProcess(self.window)
        process.setProcessChannelMode(self.QtCore.QProcess.MergedChannels)
        process.readyReadStandardOutput.connect(self._read_profile_delete_output)
        process.finished.connect(self._profile_delete_finished)
        self.profile_delete_process = process
        self._profile_delete_selected_ids = selected_ids
        self._profile_delete_remove_systemd = bool(remove_systemd)
        self.profile_list.set_runtime_actions_enabled(False)
        self.controls.start_button.setEnabled(False)
        self.controls.set_status_text("Deleting selected Auto-UV profiles.")
        process.start(command[0], command[1:])
        if not process.waitForStarted(3000):
            self.log_view.append("Failed to start Auto-UV profile delete action.\n")
            self._profile_delete_finished(-1, self.QtCore.QProcess.CrashExit)

    def _read_profile_delete_output(self) -> None:
        if self.profile_delete_process is None:
            return
        data = bytes(self.profile_delete_process.readAllStandardOutput()).decode(
            "utf-8",
            errors="replace",
        )
        self.log_view.append(data)

    def _profile_delete_finished(self, exit_code, exit_status) -> None:
        selected_ids = getattr(self, "_profile_delete_selected_ids", set())
        remove_systemd = bool(getattr(self, "_profile_delete_remove_systemd", False))
        if self.last_auto_uv_profile_id in selected_ids:
            self.last_auto_uv_profile_id = ""
            self.last_auto_uv_candidate_id = ""
        if self.profile_delete_process is not None:
            self.profile_delete_process.deleteLater()
        self.profile_delete_process = None
        self._profile_delete_remove_systemd = False
        self.profile_list.set_runtime_actions_enabled(self.process is None)
        self.controls.start_button.setEnabled(self.process is None)
        if int(exit_code) == 0:
            self.controls.set_status_text("Selected Auto-UV profiles deleted.")
            if remove_systemd:
                self.log_view.append(
                    "Removing Systemd autostart entry for deleted profile.\n"
                )
                self._run_runtime_action("uninstall-systemd")
                return
        else:
            self.controls.set_status_text("Auto-UV profile deletion failed.")
            self.log_view.append(
                f"\nAuto-UV profile deletion failed: exit_code={exit_code}\n"
            )
            self._show_process_error_dialog(
                title="Profile deletion failed",
                action_label="Delete selected profiles",
                exit_code=exit_code,
                exit_status=exit_status,
            )
        self._load_profiles()

    def _confirm_profile_delete(
        self,
        names: list[str],
        *,
        removes_systemd: bool = False,
        includes_afterburner: bool = False,
    ) -> bool:
        message = _profile_delete_confirmation_text(
            names,
            removes_systemd=removes_systemd,
            includes_afterburner=includes_afterburner,
        )
        buttons = (
            self.QtWidgets.QMessageBox.StandardButton.Yes
            | self.QtWidgets.QMessageBox.StandardButton.No
        )
        answer = self.QtWidgets.QMessageBox.question(
            self.window,
            "Delete Profiles",
            message,
            buttons,
            self.QtWidgets.QMessageBox.StandardButton.No,
        )
        return answer == self.QtWidgets.QMessageBox.StandardButton.Yes

    def _show_process_error_dialog(
        self,
        *,
        title: str,
        action_label: str,
        exit_code,
        exit_status,
        extra_details: str = "",
    ) -> None:
        details = _process_failure_details(
            action_label=action_label,
            exit_code=exit_code,
            exit_status=_qt_enum_name(exit_status),
            extra_details=extra_details,
            log_tail=self._recent_log_tail(),
        )
        self._show_error_dialog(
            title,
            (
                f"{action_label} stopped unexpectedly. "
                "The full error details can be copied for troubleshooting."
            ),
            details=details,
        )

    def _show_error_dialog(
        self,
        title: str,
        message: str,
        *,
        details: str = "",
    ) -> None:
        dialog = self.QtWidgets.QDialog(self.window)
        dialog.setWindowTitle(str(title))
        dialog.setModal(True)
        dialog.resize(760, 420)

        layout = self.QtWidgets.QVBoxLayout(dialog)
        header_layout = self.QtWidgets.QHBoxLayout()
        icon_label = self.QtWidgets.QLabel()
        icon = _critical_error_icon(self.QtGui, self.QtWidgets, dialog)
        pixmap = icon.pixmap(48, 48)
        if not pixmap.isNull():
            icon_label.setPixmap(pixmap)
        icon_label.setAlignment(
            _qt_flags(self.QtCore.Qt, "AlignmentFlag", "AlignTop", "AlignHCenter")
        )
        message_label = self.QtWidgets.QLabel(str(message))
        message_label.setWordWrap(True)
        message_label.setTextInteractionFlags(
            _qt_flags(
                self.QtCore.Qt,
                "TextInteractionFlag",
                "TextSelectableByMouse",
                "TextSelectableByKeyboard",
            )
        )
        header_layout.addWidget(icon_label)
        header_layout.addWidget(message_label, 1)
        layout.addLayout(header_layout)

        copy_text = _error_dialog_copy_text(title, message, details=details)
        detail_edit = self.QtWidgets.QPlainTextEdit()
        detail_edit.setReadOnly(True)
        detail_edit.setPlainText(copy_text)
        detail_edit.setLineWrapMode(_plain_text_no_wrap_mode(self.QtWidgets))
        detail_edit.setMinimumHeight(220)
        detail_edit.setFont(_fixed_width_font(self.QtGui))
        layout.addWidget(detail_edit, 1)

        buttons = self.QtWidgets.QDialogButtonBox(
            self.QtWidgets.QDialogButtonBox.StandardButton.Ok
        )
        copy_button = buttons.addButton(
            "Copy Error",
            self.QtWidgets.QDialogButtonBox.ButtonRole.ActionRole,
        )
        copy_button.clicked.connect(
            lambda: self.QtWidgets.QApplication.clipboard().setText(copy_text)
        )
        buttons.accepted.connect(dialog.accept)
        layout.addWidget(buttons)
        dialog.exec()

    def _recent_log_tail(self, *, lines: int = 80, char_limit: int = 12000) -> str:
        text = self.log_view.text_edit.toPlainText()
        tail = "\n".join(text.splitlines()[-max(1, int(lines)) :])
        if len(tail) > int(char_limit):
            tail = tail[-int(char_limit) :]
        return tail

    def _process_finished(self, exit_code, exit_status) -> None:
        status_name = "finished" if int(exit_code) == 0 else "stopped"
        self.log_view.append(f"\nAuto-UV process {status_name}: exit_code={exit_code}\n")
        final_result_payload = self.pending_final_result_payload
        stopped_by_user = self.stop_requested
        final_choice_discarded = self.final_choice_discarded
        missing_final_result = (
            int(exit_code) == 0
            and not stopped_by_user
            and not final_choice_discarded
            and final_result_payload is None
        )
        failed_unexpectedly = int(exit_code) != 0 or missing_final_result
        if stopped_by_user:
            self.runs_table.mark_running_rows_stopped(label="Stopped")
        elif failed_unexpectedly:
            self.runs_table.mark_running_rows_stopped(label="Failed")
        if self.header.stage() != "Complete":
            self.header.set_stage(
                "Stopped"
                if stopped_by_user
                else ("Error" if failed_unexpectedly else "Idle")
            )
        self.controls.set_running(False)
        self.controls.hide_dependency_progress()
        self.profile_list.set_runtime_actions_enabled(True)
        if self.process is not None:
            self.process.deleteLater()
        self.process = None
        self.active_stop_request_path = None
        self.stop_requested = False
        self.profile_disabled_for_scan = False
        self._load_profiles(prefer_last_auto_uv=True)
        if final_choice_discarded and int(exit_code) == 0 and not stopped_by_user:
            self.runs_table.mark_running_rows_stopped(label="Discarded")
            self.controls.set_status_text(
                "Final verification discarded. No Auto-UV profile was saved."
            )
        if failed_unexpectedly and not stopped_by_user:
            self.controls.set_status_text("Auto-UV failed.")
            self._show_process_error_dialog(
                title="Auto-UV failed",
                action_label="Auto-UV process",
                exit_code=exit_code,
                exit_status=exit_status,
                extra_details=(
                    "Auto-UV exited without reporting a final result."
                    if missing_final_result
                    else ""
                ),
            )
        if int(exit_code) == 0 and final_result_payload and not stopped_by_user:
            self._show_final_verification_complete(final_result_payload)
        self.pending_final_result_payload = None
        self.final_choice_discarded = False

    def _update_runner_status(self) -> None:
        if self.process is not None and self.profile_disabled_for_scan:
            self.controls.set_status_text(
                "Profile disabled during the auto undervolting sweep"
            )
            return
        running_info = (
            _running_auto_uv_profile_info()
            if _penguin_burner_runtime_is_active()
            else {"selector": "", "silent_fan_curve": False}
        )
        autostart_info = _systemd_autostart_profile_info()
        self.controls.set_status_text(
            _runner_status_text(
                self.profile_summaries,
                running_selector=str(running_info["selector"]),
                autostart_selector=str(autostart_info["selector"]),
                running_silent_fan=bool(running_info["silent_fan_curve"]),
                autostart_silent_fan=bool(autostart_info["silent_fan_curve"]),
            )
        )

    def _runtime_action_start_text(self, action: str) -> str:
        selected = self.profile_list.selected_profile_name() or "none"
        silent = _on_off(self.profile_list.silent_fan_enabled())
        if action == "install-systemd":
            return (
                f"Starting profile: {selected}; Systemd autostart: Yes; "
                f"Silent fan curve: {silent}."
            )
        if action == "uninstall-systemd":
            return "Removing Systemd autostart entry."
        return (
            f"Starting profile: {selected}; Systemd autostart: No; "
            f"Silent fan curve: {silent}."
        )

    def _record_fan_measurement(self, payload: dict) -> None:
        point = _fan_measurement_point(payload)
        if point is None:
            return
        self.fan_measured_points.append(point)
        self.fan_plot.set_source_points(
            _sorted_unique_fan_points(self.fan_measured_points)
        )

    def _record_fan_measurements(self, values) -> None:
        points = _fan_measurement_points(values)
        if not points:
            return
        self.fan_measured_points.extend(points)
        self.fan_plot.set_source_points(
            _sorted_unique_fan_points(self.fan_measured_points)
        )

    def _refresh_saved_fan_curve_plot(self) -> None:
        payload = _saved_auto_uv_fan_curve_payload()
        if payload is None:
            return
        self._populate_fan_plot_from_payload(payload)

    def _populate_fan_plot_from_payload(self, payload: dict) -> None:
        points = _fan_curve_points_from_payload(payload)
        if not points:
            return
        measured_points = _fan_measurement_points_from_payload(payload)
        if measured_points:
            self.fan_measured_points = list(measured_points)
            self.fan_plot.set_source_points(measured_points)
        self.fan_plot.set_candidate_points(points, remember_previous=False)
        target = _fan_curve_target_point_from_payload(payload)
        if target is not None:
            self.fan_plot.set_selected_point(target[0], target[1])

    def _sync_selected_profile_fan_curve_payload(self, profile: dict) -> None:
        payload = _profile_fan_payload(profile)
        if payload is None or not _fan_payload_has_silent_runtime_fields(payload):
            return
        _write_auto_uv_fan_curve_payload(payload)
        self._populate_fan_plot_from_payload(payload)

    def _show_final_verification_complete(self, payload: dict) -> None:
        self._load_profiles(prefer_last_auto_uv=True, select_last_auto_uv=True)
        self.tabs.setCurrentIndex(self.profiles_tab_index)
        self.profile_list.table.setFocus()
        message = _final_profile_notice_text(
            self.profile_summaries,
            profile_id=self.last_auto_uv_profile_id,
            candidate_id=self.last_auto_uv_candidate_id,
            result_payload=payload,
        )
        self.controls.set_status_text(message)
        self.QtWidgets.QMessageBox.information(
            self.window,
            "Final verification complete",
            message,
        )

    def _handle_final_choice_request(self, payload: dict) -> None:
        candidates = [
            dict(candidate)
            for candidate in payload.get("candidates", [])
            if isinstance(candidate, dict)
        ]
        auto_uv_mode = str(payload.get("auto_uv_mode", ""))
        candidates = _sort_candidates_for_final_choice(candidates, auto_uv_mode)
        default_id = _best_final_choice_candidate_id(candidates, auto_uv_mode) or str(
            payload.get("default_candidate_id", "")
        )
        default_sort_column = _final_choice_sort_column_for_mode(auto_uv_mode)
        if _is_performance_mode(auto_uv_mode):
            selection_text = (
                "The short candidate sweep is complete. The highest-FPS passed "
                "candidate is selected for the long Final verification. Pick the "
                "profile and final check duration before starting the final run."
            )
        else:
            selection_text = (
                "The short candidate sweep is complete. The best FPS/W passed "
                "candidate is selected for the long Final verification. Pick the "
                "profile and final check duration before starting the final run."
            )
        default_duration_s = _duration_seconds_from_value(
            payload.get("final_verification_duration_s"),
            default_s=DEFAULT_FINAL_VERIFICATION_DURATION_S,
        )
        max_duration_s = _duration_seconds_from_value(
            payload.get("max_final_verification_duration_s"),
            default_s=MAX_FINAL_VERIFICATION_DURATION_S,
        )
        selected, final_duration_s, discarded = self._select_candidate_dialog(
            title="Choose Final verification candidate",
            text=selection_text,
            candidates=candidates,
            default_candidate_id=default_id,
            default_final_duration_s=default_duration_s,
            max_final_duration_s=max_duration_s,
            default_sort_column=default_sort_column,
            auto_uv_mode=auto_uv_mode,
        )
        response_path = Path(str(payload.get("response_path", ""))).expanduser()
        if not str(response_path).strip():
            return
        response_path.parent.mkdir(parents=True, exist_ok=True)
        if discarded:
            self.last_auto_uv_candidate_id = ""
            self.last_auto_uv_profile_id = ""
            self.final_choice_discarded = True
            self.header.set_stage("Discarded")
            self.header.set_candidate("")
            self.controls.set_status_text(
                "Final verification discarded. No Auto-UV profile was saved."
            )
            response_path.write_text(
                json.dumps({"action": "discard"}, indent=2) + "\n",
                encoding="utf-8",
            )
            self.log_view.append(
                "Final verification discarded by user; no profile will be saved.\n"
            )
            return
        if selected is None:
            selected_id = default_id
        else:
            selected_id = str(selected.get("candidate_id", ""))
        self.last_auto_uv_candidate_id = selected_id
        self.last_auto_uv_profile_id = ""
        response_path.write_text(
            json.dumps(
                {
                    "candidate_id": selected_id,
                    "final_verification_duration_s": int(final_duration_s),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        self.log_view.append(
            "Selected Final verification candidate: "
            f"{selected_id}; duration: {_format_duration_for_user(final_duration_s)}\n"
        )

    def _select_candidate_dialog(
        self,
        *,
        title: str,
        text: str,
        candidates: list[dict],
        default_candidate_id: str,
        default_final_duration_s: int,
        max_final_duration_s: int,
        default_sort_column: int = FINAL_CHOICE_DEFAULT_SORT_COLUMN,
        auto_uv_mode: object = "",
    ) -> tuple[dict | None, int, bool]:
        if not candidates:
            return None, int(default_final_duration_s), True
        by_id = {
            str(candidate.get("candidate_id", "")): candidate
            for candidate in candidates
        }
        dialog = self.QtWidgets.QDialog(self.window)
        dialog.setWindowTitle(title)
        layout = self.QtWidgets.QVBoxLayout(dialog)
        label = self.QtWidgets.QLabel(text)
        label.setWordWrap(True)
        item_class = _sortable_table_item_class(
            self.QtWidgets,
            FINAL_CHOICE_SORT_ROLE,
        )
        table = self.QtWidgets.QTableWidget(len(candidates), 8)
        table.setHorizontalHeaderLabels(
            [
                "mV",
                "Target MHz",
                "Effective MHz",
                "FPS/W",
                "FPS",
                "Power W",
                "Short Check",
                "Status",
            ]
        )
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(self.QtWidgets.QAbstractItemView.SelectRows)
        table.setEditTriggers(self.QtWidgets.QAbstractItemView.NoEditTriggers)
        table.setSortingEnabled(False)
        header = table.horizontalHeader()
        header.setHighlightSections(False)
        header.setSectionsClickable(True)
        set_header_fit_column_widths(
            table,
            {
                0: 62,
                1: 108,
                2: 128,
                3: 82,
                4: 76,
                5: 92,
                6: 104,
                7: 110,
            },
            QtCore=self.QtCore,
            padding=34,
        )
        table.horizontalHeader().setStretchLastSection(True)
        for row, candidate in enumerate(candidates):
            candidate_id = str(candidate.get("candidate_id", ""))
            values = [
                _candidate_number(candidate.get("candidate_voltage_mv"), precision=0),
                _candidate_number(candidate.get("lock_clock_mhz"), precision=0),
                _candidate_number(candidate.get("avg_core_clock_mhz"), precision=2),
                _candidate_number(candidate.get("efficiency_fps_per_w"), precision=2),
                _candidate_number(candidate.get("avg_fps"), precision=2),
                _candidate_number(candidate.get("avg_power_w"), precision=2),
                _format_duration_for_user(_candidate_short_duration_s(candidate)),
                _candidate_status_text(
                    candidate,
                    candidate_id == default_candidate_id,
                    auto_uv_mode=auto_uv_mode,
                ),
            ]
            sort_values = _final_choice_sort_values(candidate)
            for column, value in enumerate(values):
                item = item_class(str(value))
                item.setData(self.QtCore.Qt.UserRole, candidate_id)
                item.setData(FINAL_CHOICE_SORT_ROLE, sort_values[column])
                if column < 6:
                    item.setTextAlignment(
                        self.QtCore.Qt.AlignRight | self.QtCore.Qt.AlignVCenter
                    )
                if candidate_id == default_candidate_id:
                    item.setBackground(self.QtGui.QColor("#263c2f"))
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                table.setItem(row, column, item)
        table.doubleClicked.connect(dialog.accept)
        sort_column = int(default_sort_column)
        sort_order = self.QtCore.Qt.DescendingOrder

        def apply_sort_indicator(column: int, order) -> None:
            signals_blocked = header.blockSignals(True)
            try:
                header.setSortIndicatorShown(True)
                header.setSortIndicator(int(column), order)
            finally:
                header.blockSignals(signals_blocked)

        def sort_table(column: int, order) -> None:
            nonlocal sort_column, sort_order
            if int(column) not in FINAL_CHOICE_SORTABLE_COLUMNS:
                apply_sort_indicator(sort_column, sort_order)
                return
            sort_column = int(column)
            sort_order = order
            apply_sort_indicator(sort_column, sort_order)
            item_class.sort_descending = order == self.QtCore.Qt.DescendingOrder
            table.sortItems(sort_column, sort_order)

        def sort_by_header_column(column: int) -> None:
            nonlocal sort_column, sort_order
            column = int(column)
            if column not in FINAL_CHOICE_SORTABLE_COLUMNS:
                apply_sort_indicator(sort_column, sort_order)
                return
            if sort_column == column:
                order = (
                    self.QtCore.Qt.AscendingOrder
                    if sort_order == self.QtCore.Qt.DescendingOrder
                    else self.QtCore.Qt.DescendingOrder
                )
            else:
                order = (
                    self.QtCore.Qt.DescendingOrder
                    if column in FINAL_CHOICE_HIGHER_FIRST_COLUMNS
                    else self.QtCore.Qt.AscendingOrder
                )
            sort_table(column, order)

        header.sectionClicked.connect(sort_by_header_column)
        sort_table(sort_column, sort_order)
        max_minutes = _duration_minutes_for_control(max_final_duration_s)
        duration_spin = self.QtWidgets.QSpinBox()
        duration_spin.setRange(1, max_minutes)
        duration_spin.setSuffix(" min")
        duration_spin.setSingleStep(1)
        duration_spin.setValue(_duration_minutes_for_control(default_final_duration_s))
        duration_hint = self.QtWidgets.QLabel("")
        duration_hint.setObjectName("durationHint")

        def selected_candidate() -> dict | None:
            selected_rows = table.selectionModel().selectedRows()
            if not selected_rows:
                return by_id.get(default_candidate_id)
            selected_row = int(selected_rows[-1].row())
            item = table.item(selected_row, 0)
            selected_id = (
                str(item.data(self.QtCore.Qt.UserRole) or "")
                if item is not None
                else default_candidate_id
            )
            return by_id.get(selected_id)

        def sync_duration_constraints() -> None:
            candidate = selected_candidate() or {}
            min_duration_s = _candidate_short_duration_s(candidate)
            min_minutes = min(
                max_minutes,
                _duration_minutes_for_control(min_duration_s),
            )
            duration_spin.setMinimum(min_minutes)
            if duration_spin.value() < min_minutes:
                duration_spin.setValue(min_minutes)
            duration_hint.setText(
                "Minimum "
                f"{_format_duration_for_user(min_duration_s)} "
                "from the selected short check; maximum "
                f"{_format_duration_for_user(max_final_duration_s)}."
            )

        table.itemSelectionChanged.connect(sync_duration_constraints)

        if default_candidate_id in by_id:
            for row in range(table.rowCount()):
                item = table.item(row, 0)
                if (
                    item is not None
                    and str(item.data(self.QtCore.Qt.UserRole)) == default_candidate_id
                ):
                    table.selectRow(row)
                    break
        elif table.rowCount() > 0:
            table.selectRow(0)
        sync_duration_constraints()

        duration_layout = self.QtWidgets.QHBoxLayout()
        duration_layout.addWidget(self.QtWidgets.QLabel("Final verification duration"))
        duration_layout.addWidget(duration_spin)
        duration_layout.addStretch(1)
        layout.addWidget(label)
        layout.addWidget(table)
        layout.addLayout(duration_layout)
        layout.addWidget(duration_hint)
        buttons = self.QtWidgets.QDialogButtonBox()
        discard_button = buttons.addButton(
            "Discard",
            self.QtWidgets.QDialogButtonBox.DestructiveRole,
        )
        discard_button.setObjectName("discardFinalChoiceButton")
        keep_button = buttons.addButton(
            "Use Selected",
            self.QtWidgets.QDialogButtonBox.AcceptRole,
        )

        def handle_button(button) -> None:
            if button is discard_button:
                dialog.reject()
            elif button is keep_button:
                dialog.accept()

        buttons.clicked.connect(handle_button)
        layout.addWidget(buttons)
        dialog.setMinimumWidth(780)
        dialog.setMinimumHeight(360)
        keep_button.setDefault(True)
        selected_duration_s = lambda: _duration_seconds_from_minutes(
            duration_spin.value()
        )
        if dialog.exec() != self.QtWidgets.QDialog.Accepted:
            return None, selected_duration_s(), True
        selected_rows = table.selectionModel().selectedRows()
        if selected_rows:
            selected_row = int(selected_rows[-1].row())
        else:
            selected_row = 0
        item = table.item(selected_row, 0)
        selected_id = (
            str(item.data(self.QtCore.Qt.UserRole) or "")
            if item is not None
            else default_candidate_id
        )
        return by_id.get(selected_id), selected_duration_s(), False


def _event_points(payload: dict) -> list[tuple[float, float]]:
    points = payload.get("points")
    if not isinstance(points, list):
        return []
    converted = []
    for point in points:
        if not isinstance(point, dict):
            continue
        voltage = point.get("voltage_mv")
        clock = point.get("clock_mhz")
        if voltage is None or clock is None:
            continue
        converted.append((float(voltage), float(clock)))
    return converted


def _event_base_points(payload: dict) -> list[tuple[float, float]]:
    points = _base_curve_points_from_values(payload.get("points"))
    return points or _event_points(payload)


def _candidate_display_text(candidate: dict) -> str:
    voltage = candidate.get("candidate_voltage_mv", "n/a")
    clock = candidate.get("lock_clock_mhz", "n/a")
    parts = [f"{voltage} mV @ {clock} MHz"]
    measured = candidate.get("avg_core_clock_mhz")
    if measured is not None:
        parts.append(f"measured {float(measured):.2f} MHz")
    fps_per_w = candidate.get("efficiency_fps_per_w")
    if fps_per_w is not None:
        parts.append(f"FPS/W {float(fps_per_w):.2f}")
    if bool(candidate.get("final_verified")):
        parts.append("Final stability verified")
    else:
        parts.append("Passed short probe")
    return " | ".join(parts)


def _candidate_status_text(
    candidate: dict,
    is_default: bool,
    *,
    auto_uv_mode: object = "",
) -> str:
    parts = []
    if is_default:
        parts.append("Best FPS" if _is_performance_mode(auto_uv_mode) else "Best FPS/W")
    if bool(candidate.get("final_verified")):
        parts.append("Final stability verified")
    else:
        parts.append("Passed short probe")
    return " | ".join(parts)


def _candidate_number(value, *, precision: int) -> str:
    if value in (None, ""):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    precision = max(0, min(int(precision), 2))
    if precision <= 0:
        return str(int(round(number)))
    return f"{number:.{precision}f}"


def _status_value(value, *, precision: int = 2) -> str:
    if value in (None, ""):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(number):
        return ""
    if number.is_integer():
        return str(int(round(number)))
    precision = max(0, min(int(precision), 2))
    return f"{number:.{precision}f}"


def _duration_seconds_from_value(
    value,
    *,
    default_s: int = DEFAULT_FINAL_VERIFICATION_DURATION_S,
) -> int:
    try:
        duration_s = int(round(float(value)))
    except (TypeError, ValueError):
        duration_s = int(default_s)
    return max(1, min(MAX_FINAL_VERIFICATION_DURATION_S, int(duration_s)))


def _duration_minutes_for_control(seconds) -> int:
    try:
        duration_s = max(1, int(round(float(seconds))))
    except (TypeError, ValueError):
        duration_s = DEFAULT_FINAL_VERIFICATION_DURATION_S
    return max(
        1,
        min(
            MAX_FINAL_VERIFICATION_DURATION_S // 60,
            int(math.ceil(float(duration_s) / 60.0)),
        ),
    )


def _duration_seconds_from_minutes(minutes) -> int:
    try:
        value = int(minutes)
    except (TypeError, ValueError):
        value = _duration_minutes_for_control(DEFAULT_FINAL_VERIFICATION_DURATION_S)
    return max(60, min(MAX_FINAL_VERIFICATION_DURATION_S, int(value) * 60))


def _candidate_short_duration_s(candidate: dict) -> int:
    value = candidate.get("short_verification_duration_s")
    return _duration_seconds_from_value(value, default_s=30)


def _final_choice_sort_values(candidate: dict) -> list[float | str]:
    return [
        _numeric_sort_value(candidate.get("candidate_voltage_mv")),
        _numeric_sort_value(candidate.get("lock_clock_mhz")),
        _numeric_sort_value(candidate.get("avg_core_clock_mhz")),
        _numeric_sort_value(candidate.get("efficiency_fps_per_w")),
        _numeric_sort_value(candidate.get("avg_fps")),
        _numeric_sort_value(candidate.get("avg_power_w")),
        float(_candidate_short_duration_s(candidate)),
        str(_candidate_status_text(candidate, False)).casefold(),
    ]


def _numeric_sort_value(value) -> float | str:
    if value in (None, ""):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(number):
        return ""
    return float(number)


def _sortable_table_item_class(QtWidgets, sort_role: int):
    class SortableTableWidgetItem(QtWidgets.QTableWidgetItem):
        sort_descending = False

        def __lt__(self, other):
            other_value = (
                other.data(sort_role) if hasattr(other, "data") else str(other.text())
            )
            return _sort_value_less(
                self.data(sort_role),
                other_value,
                descending=bool(self.sort_descending),
            )

    return SortableTableWidgetItem


def _sort_value_less(left, right, *, descending: bool = False) -> bool:
    left_missing = left in (None, "")
    right_missing = right in (None, "")
    if left_missing != right_missing:
        return left_missing if descending else right_missing
    if left_missing and right_missing:
        return False
    left_key = _sort_key(left)
    right_key = _sort_key(right)
    return left_key < right_key


def _sort_key(value) -> float | str:
    number = _numeric_sort_value(value)
    if number != "":
        return float(number)
    return str(value).casefold()


_REVERIFY_ELAPSED_RE = re.compile(r"\belapsed=([0-9]+(?:\.[0-9]+)?)s\b")


def _reverify_elapsed_from_line(line: str) -> float | None:
    match = _REVERIFY_ELAPSED_RE.search(str(line or ""))
    if match is None:
        return None
    try:
        return max(0.0, float(match.group(1)))
    except ValueError:
        return None


def _reverify_progress_percent(elapsed_s, target_s) -> int:
    try:
        elapsed = max(0.0, float(elapsed_s))
        target = max(1.0, float(target_s))
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, int(round((elapsed / target) * 100.0))))


def _reverify_workload_label(
    *,
    q2rtx_enabled: bool = True,
    cuda_enabled: bool = True,
) -> str:
    if q2rtx_enabled and cuda_enabled:
        return "Q2RTX timedemo and CUDA compute test"
    if q2rtx_enabled:
        return "Q2RTX timedemo"
    if cuda_enabled:
        return "CUDA compute test"
    return "No workload"


_DECIMAL_TEXT_RE = re.compile(
    r"(?<![A-Za-z0-9_.+-])([+-]?\d+\.\d+(?:[eE][+-]?\d+)?)(?![\d.])"
)


def _round_gui_decimals(text: str, *, precision: int = 2) -> str:
    precision = max(0, min(int(precision), 2))

    def replace(match: re.Match[str]) -> str:
        raw = match.group(1)
        try:
            number = float(raw)
        except ValueError:
            return raw
        if not math.isfinite(number):
            return raw
        return f"{number:.{precision}f}"

    return _DECIMAL_TEXT_RE.sub(replace, str(text or ""))


def _top_status_text(text: str, *, limit: int | None = None) -> str:
    rounded = _round_gui_decimals(str(text or "").strip())
    if limit is None or limit <= 0:
        return rounded
    return rounded[: int(limit)]


def _candidate_fpsw(candidate: dict) -> float | None:
    value = candidate.get("efficiency_fps_per_w")
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _candidate_fps(candidate: dict) -> float | None:
    value = candidate.get("avg_fps")
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_performance_mode(auto_uv_mode: object) -> bool:
    return str(auto_uv_mode or "").strip().lower() == AUTO_UV_MODE_PERFORMANCE


def _final_choice_sort_column_for_mode(auto_uv_mode: object) -> int:
    if _is_performance_mode(auto_uv_mode):
        return FINAL_CHOICE_FPS_SORT_COLUMN
    return FINAL_CHOICE_FPSW_SORT_COLUMN


def _sort_candidates_by_fpsw(candidates: list[dict]) -> list[dict]:
    return sorted(
        candidates,
        key=lambda candidate: (
            _candidate_fpsw(candidate) is None,
            -float(_candidate_fpsw(candidate) or 0.0),
            int(candidate.get("candidate_voltage_mv") or 99999),
            -int(candidate.get("lock_clock_mhz") or 0),
        ),
    )


def _sort_candidates_by_fps(candidates: list[dict]) -> list[dict]:
    return sorted(
        candidates,
        key=lambda candidate: (
            _candidate_fps(candidate) is None,
            -float(_candidate_fps(candidate) or 0.0),
            int(candidate.get("candidate_voltage_mv") or 99999),
            -int(candidate.get("lock_clock_mhz") or 0),
        ),
    )


def _sort_candidates_for_final_choice(
    candidates: list[dict],
    auto_uv_mode: object,
) -> list[dict]:
    if _is_performance_mode(auto_uv_mode):
        return _sort_candidates_by_fps(candidates)
    return _sort_candidates_by_fpsw(candidates)


def _best_fpsw_candidate_id(candidates: list[dict]) -> str:
    for candidate in candidates:
        if _candidate_fpsw(candidate) is not None:
            return str(candidate.get("candidate_id", ""))
    return str(candidates[0].get("candidate_id", "")) if candidates else ""


def _best_fps_candidate_id(candidates: list[dict]) -> str:
    for candidate in candidates:
        if _candidate_fps(candidate) is not None:
            return str(candidate.get("candidate_id", ""))
    return str(candidates[0].get("candidate_id", "")) if candidates else ""


def _best_final_choice_candidate_id(
    candidates: list[dict],
    auto_uv_mode: object,
) -> str:
    if _is_performance_mode(auto_uv_mode):
        return _best_fps_candidate_id(candidates)
    return _best_fpsw_candidate_id(candidates)


def _candidate_id_from_result(payload: dict) -> str:
    voltage = payload.get("voltage_mv")
    clock = payload.get("clock_mhz")
    if voltage in (None, "") or clock in (None, ""):
        return ""
    try:
        return f"{int(float(voltage))}mv-{int(float(clock))}mhz"
    except (TypeError, ValueError):
        return ""


def _profile_id_for_candidate(profiles: list[dict], candidate_id: str) -> str:
    text = str(candidate_id or "").strip()
    if not text:
        return ""
    for profile in profiles:
        if str(profile.get("candidate_id", "")) == text:
            return str(profile.get("profile_id", ""))
    return ""


def _selected_profile_ids_include_selector(
    profiles: list[dict],
    selected_profile_ids: list[str],
    selector: str,
) -> bool:
    selected = {
        str(profile_id).strip()
        for profile_id in selected_profile_ids
        if str(profile_id).strip()
    }
    if not selected:
        return False
    profile = _profile_for_selector(profiles, selector)
    if profile is None:
        return False
    return str(profile.get("profile_id", "")).strip() in selected


def _profiles_for_selectors(profiles: list[dict], selectors: list[str]) -> list[dict]:
    selected_profiles = []
    seen = set()
    for selector in selectors:
        profile = _profile_for_selector(profiles, selector)
        if profile is None:
            continue
        profile_id = str(profile.get("profile_id", "")).strip()
        key = profile_id or str(id(profile))
        if key in seen:
            continue
        selected_profiles.append(profile)
        seen.add(key)
    return selected_profiles


def _profile_is_afterburner(profile: dict) -> bool:
    return str(profile.get("runtime_source", "")).strip() == "afterburner"


def _profile_is_verified(profile: dict) -> bool:
    return bool(profile.get("final_verified", False))


def _profile_can_apply(profile: dict) -> bool:
    return _profile_is_afterburner(profile) or _profile_is_verified(profile)


def _profile_is_deletable(profile: dict) -> bool:
    return bool(str(profile.get("path", "")).strip()) or _profile_is_afterburner(
        profile
    )


def _profile_can_export_lact(profile: dict) -> bool:
    return _profile_can_apply(profile) and bool(
        _profile_is_afterburner(profile) or str(profile.get("path", "")).strip()
    )


def _profile_can_reverify(profile: dict) -> bool:
    return _profile_is_afterburner(profile) or bool(str(profile.get("path", "")).strip())


def _profile_verify_selector(profile: dict) -> str:
    if _profile_is_afterburner(profile):
        return ""
    path = str(profile.get("path", "")).strip()
    if path:
        return path
    return str(profile.get("profile_id", "")).strip() or path


def _profile_id_from_archive_path(path: str | Path) -> str:
    name = Path(path).name
    prefix = "auto-uv-profile-"
    suffix = ".json"
    if name.startswith(prefix) and name.endswith(suffix):
        return name[len(prefix) : -len(suffix)]
    return Path(path).stem


def _lact_export_output_path(directory: str | Path) -> Path:
    return Path(directory).expanduser() / LACT_CONFIG_FILENAME


def _lact_gpu_id_from_config(path: str | Path) -> str:
    path = Path(path).expanduser()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    in_gpus = False
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not in_gpus:
            if stripped == "gpus:":
                in_gpus = True
            continue
        if not line.startswith((" ", "\t")):
            return ""
        if not line.startswith("  ") or line.startswith("    "):
            continue
        key = stripped.rsplit(":", 1)[0].strip().strip("'\"")
        if key:
            return key
    return ""


def _detect_lact_gpu_id(output_dir: str | Path) -> str:
    directory = Path(output_dir).expanduser()
    for config_path in (
        directory / LACT_CONFIG_FILENAME,
        Path("/etc/lact") / LACT_CONFIG_FILENAME,
    ):
        gpu_id = _lact_gpu_id_from_config(config_path)
        if gpu_id:
            return gpu_id
    return ""


def _current_fan_config() -> dict:
    try:
        config = load_config(default_runtime_config_path())
    except Exception:
        return {}
    fan = config.get("fan", {}) if isinstance(config, dict) else {}
    return dict(fan) if isinstance(fan, dict) else {}


def _write_lact_profile_config(
    profile: dict,
    *,
    output_path: Path,
    gpu_id: str,
    include_fan_curve: bool = False,
) -> tuple[Path, list[str]]:
    if _profile_is_afterburner(profile):
        options = load_afterburner_runtime_options(default_runtime_config_path())
        afterburner_root = str(
            profile.get("afterburner_root") or options.get("afterburner_root") or ""
        ).strip()
        if not afterburner_root:
            raise LactExportError(
                "Afterburner root is not configured for this profile"
            )
        return write_lact_nvidia_config_from_afterburner(
            output_path=output_path,
            gpu_id=gpu_id,
            current_fan_config=_current_fan_config(),
            gpu_index=_runtime_gpu_index(default_runtime_config_path()),
            afterburner_root=afterburner_root,
            section=profile.get("afterburner_profile")
            or options.get("afterburner_profile")
            or None,
            device_profile_hint=profile.get("afterburner_device_profile")
            or options.get("afterburner_device_profile")
            or None,
            dangerously_skip_validation=bool(
                options.get("dangerously_skip_validation")
            ),
            preserve_base_below_mv=options.get("preserve_base_below_mv"),
            include_vf_curve=True,
            include_fan_curve=include_fan_curve,
        )
    profile_path = Path(str(profile.get("path", "")).strip()).expanduser()
    if not profile_path.is_file():
        raise LactExportError("Selected Auto-UV profile file was not found")
    return write_lact_nvidia_config(
        output_path=output_path,
        gpu_id=gpu_id,
        final_curve_path=profile_path,
        include_vf_curve=True,
        include_fan_curve=include_fan_curve,
    )


def _delete_afterburner_import_config() -> bool:
    config_path = default_runtime_config_path()
    config = load_config(config_path)
    gpu = dict(config.get("gpu", {})) if isinstance(config, dict) else {}
    removed = any(
        str(gpu.get(key, "")).strip()
        for key in ("afterburner_profile", "afterburner_device_profile")
    )
    gpu.pop("afterburner_profile", None)
    gpu.pop("afterburner_device_profile", None)
    updated = dict(config) if isinstance(config, dict) else {}
    if gpu:
        updated["gpu"] = gpu
    else:
        updated.pop("gpu", None)
    write_config(config_path, updated)
    return bool(removed)


def _profile_delete_confirmation_text(
    names: list[str],
    *,
    removes_systemd: bool = False,
    includes_afterburner: bool = False,
) -> str:
    count = len(names)
    if count == 1:
        label = "profile" if includes_afterburner else "Auto-UV profile"
        message = f"Delete {label} {names[0]}?"
    else:
        label = "profiles" if includes_afterburner else "Auto-UV profiles"
        message = f"Delete {count} selected {label}?"
    if includes_afterburner:
        message += (
            "\nAuto-UV profile files are removed from disk. "
            "Afterburner import entries are removed from PenguinBurner's config."
        )
    else:
        message += "\nThis removes the saved profile files from PenguinBurner."
    if removes_systemd:
        message += (
            "\nThe selected profile is currently persisted on startup. "
            "Deleting it will also stop PenguinBurner and remove the Systemd "
            "autostart entry."
        )
    return message


def _configured_afterburner_root() -> str:
    try:
        options = load_afterburner_runtime_options(default_runtime_config_path())
    except Exception:
        return ""
    return str(options.get("afterburner_root", "")).strip()


def _afterburner_profile_entries(afterburner_root: str | Path) -> list[dict]:
    root = resolve_afterburner_root(afterburner_root).expanduser()
    missing = []
    if not (root / "MSIAfterburner.cfg").is_file():
        missing.append("MSIAfterburner.cfg")
    if not (root / "Profiles").is_dir():
        missing.append("Profiles/")
    if missing:
        raise FileNotFoundError(
            "Invalid Afterburner directory: missing "
            + ", ".join(missing)
            + f" under {root}"
        )

    entries: list[dict] = []
    for device_profile in discover_afterburner_device_profiles(root):
        for section in discover_afterburner_vf_sections(device_profile):
            if bool(section.get("is_builtin")):
                continue
            status, importable = _afterburner_profile_status(section)
            entries.append(
                {
                    "afterburner_root": str(root),
                    "profile_path": str(Path(device_profile).resolve()),
                    "device_profile_relative_path": _relative_profile_path(
                        root,
                        device_profile,
                    ),
                    "device_profile_name": Path(device_profile).name,
                    "section": str(section.get("section", "")),
                    "target": _afterburner_profile_target_text(section),
                    "target_voltage_mv": _afterburner_profile_target_value(
                        section,
                        "lock_voltage_mv",
                    ),
                    "target_clock_mhz": _afterburner_profile_target_value(
                        section,
                        "lock_clock_mhz",
                    ),
                    "curve_points": _afterburner_section_curve_points(section),
                    "status": status,
                    "importable": bool(importable),
                }
            )
    entries.sort(
        key=lambda entry: (
            0 if bool(entry.get("importable")) else 1,
            str(entry.get("device_profile_name", "")).lower(),
            str(entry.get("section", "")).lower(),
        )
    )
    return entries


def _afterburner_profile_target_text(section: dict) -> str:
    target = section.get("flatten_target")
    if not isinstance(target, dict):
        return ""
    try:
        clock = int(round(float(target["lock_clock_mhz"])))
        voltage = int(round(float(target["lock_voltage_mv"])))
    except (KeyError, TypeError, ValueError):
        return ""
    return f"{clock} MHz {voltage} mV"


def _afterburner_profile_target_value(section: dict, key: str) -> int | None:
    target = section.get("flatten_target")
    if not isinstance(target, dict):
        return None
    try:
        return int(round(float(target[key])))
    except (KeyError, TypeError, ValueError):
        return None


def _afterburner_section_curve_points(section: dict) -> list[tuple[float, float]]:
    materialization = section.get("materialization")
    raw_points = (
        materialization.get("points")
        if isinstance(materialization, dict)
        else section.get("points")
    )
    if not isinstance(raw_points, list):
        return []
    points = []
    for point in raw_points:
        if not isinstance(point, dict):
            continue
        voltage = point.get("voltage_mv")
        clock = point.get("frequency_mhz", point.get("target_mhz"))
        try:
            points.append((float(voltage), float(clock)))
        except (TypeError, ValueError):
            continue
    return points


def _entry_curve_points(entry: dict) -> list[tuple[float, float]]:
    raw_points = entry.get("curve_points")
    if not isinstance(raw_points, list):
        return []
    points = []
    for point in raw_points:
        try:
            points.append((float(point[0]), float(point[1])))
        except (IndexError, TypeError, ValueError):
            continue
    return points


def _profile_curve_points(profile: dict) -> list[tuple[float, float]]:
    points = _curve_points_from_payload(profile)
    if points:
        return points
    payload = _profile_payload_from_path(profile)
    if payload is None:
        return []
    return _curve_points_from_payload(payload)


def _profile_curve_plan(profile: dict) -> list[dict]:
    plan = _curve_plan_from_payload(profile)
    if plan:
        return plan
    payload = _profile_payload_from_path(profile)
    if payload is None:
        return []
    return _curve_plan_from_payload(payload)


def _profile_payload_has_saved_vf_curve(payload: dict) -> bool:
    return bool(_curve_plan_from_payload(payload) or _curve_points_from_payload(payload))


def _profile_base_curve_points(profile: dict) -> list[tuple[float, float]]:
    points = _base_curve_points_from_payload(profile)
    if points:
        return points
    payload = _profile_payload_from_path(profile)
    if payload is None:
        return []
    return _base_curve_points_from_payload(payload)


def _profile_fan_curve_points(profile: dict) -> list[tuple[float, float]]:
    payload = _profile_fan_payload(profile)
    if payload is None:
        return []
    return _fan_curve_points_from_payload(payload)


def _profile_fan_measurement_points(profile: dict) -> list[tuple[float, float]]:
    payload = _profile_fan_payload(profile)
    if payload is None:
        return []
    return _fan_measurement_points_from_payload(payload)


def _profile_fan_curve_target_point(profile: dict) -> tuple[float, float] | None:
    payload = _profile_fan_payload(profile)
    if payload is None:
        return None
    return _fan_curve_target_point_from_payload(payload)


def _profile_fan_payload(profile: dict) -> dict | None:
    if _profile_is_afterburner(profile):
        return _afterburner_fan_payload(profile)
    for payload in (
        profile,
        _profile_payload_from_path(profile),
        _matching_current_auto_uv_fan_payload(profile),
    ):
        fan_payload = _embedded_fan_payload(payload)
        if fan_payload is not None:
            return fan_payload
    return None


def _embedded_fan_payload(payload) -> dict | None:
    if not isinstance(payload, dict):
        return None
    for key in (
        "fan_curve_payload",
        "fan_curve",
        "fan_tuning",
        "auto_uv_fan_curve",
    ):
        value = payload.get(key)
        if isinstance(value, dict) and _fan_curve_points_from_payload(value):
            return dict(value)
    if _fan_curve_points_from_payload(payload):
        return dict(payload)
    return None


def _matching_current_auto_uv_fan_payload(profile: dict) -> dict | None:
    payload = _read_json_file(default_user_config_dir() / "auto-uv-fan-curve.json")
    if not isinstance(payload, dict):
        return None
    if not _fan_payload_matches_profile(payload, profile):
        return None
    return payload


def _fan_payload_matches_profile(payload: dict, profile: dict) -> bool:
    try:
        profile_voltage_mv = int(round(float(profile.get("candidate_voltage_mv"))))
        profile_clock_mhz = int(round(float(profile.get("lock_clock_mhz"))))
    except (TypeError, ValueError):
        return False
    telemetry = payload.get("telemetry")
    measured_points = (
        telemetry.get("measured_fan_points")
        if isinstance(telemetry, dict)
        else payload.get("measured_points")
    )
    if not isinstance(measured_points, list):
        return False
    for point in measured_points:
        if not isinstance(point, dict):
            continue
        try:
            voltage_mv = int(round(float(point.get("voltage_mv"))))
            clock_mhz = int(round(float(point.get("clock_mhz"))))
        except (TypeError, ValueError):
            continue
        if voltage_mv == profile_voltage_mv and clock_mhz == profile_clock_mhz:
            return True
    return False


def _afterburner_fan_payload(profile: dict) -> dict | None:
    root = str(profile.get("afterburner_root", "")).strip()
    if not root:
        try:
            options = load_afterburner_runtime_options(default_runtime_config_path())
        except Exception:
            options = {}
        root = str(options.get("afterburner_root", "")).strip()
    if not root:
        return None
    try:
        settings = load_afterburner_fan_settings(root)
    except Exception:
        return None
    curve_points = _fan_curve_points_from_values(
        settings.get("curve", {}).get("points")
    )
    if not curve_points:
        return None
    reference_points = _fan_curve_points_from_values(
        settings.get("curve2", {}).get("points")
    )
    return {
        "source": "MSI Afterburner",
        "fan": {"curve": curve_points},
        "reference_curve": reference_points,
        "profile_path": str(settings.get("profile_path", "")),
    }


def _profile_payload_from_path(profile: dict) -> dict | None:
    path_text = str(profile.get("path", "")).strip()
    if not path_text:
        return None
    path = Path(path_text).expanduser()
    return _read_json_file(path)


def _read_json_file(path: str | Path) -> dict | None:
    try:
        payload = json.loads(
            Path(path).expanduser().read_text(encoding="utf-8", errors="replace")
        )
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _curve_points_from_payload(payload: dict) -> list[tuple[float, float]]:
    if not isinstance(payload, dict):
        return []
    for key in ("curve_points", "points", "plan"):
        points = _curve_points_from_values(payload.get(key))
        if points:
            return points
    materialization = payload.get("materialization")
    if isinstance(materialization, dict):
        return _curve_points_from_values(materialization.get("points"))
    return []


def _curve_plan_from_payload(payload: dict) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    for key in ("plan", "points"):
        plan = _curve_plan_from_values(payload.get(key))
        if plan:
            return plan
    materialization = payload.get("materialization")
    if isinstance(materialization, dict):
        return _curve_plan_from_values(materialization.get("points"))
    return []


def _base_curve_points_from_payload(payload: dict) -> list[tuple[float, float]]:
    if not isinstance(payload, dict):
        return []
    for key in ("points", "plan", "curve_points"):
        points = _base_curve_points_from_values(payload.get(key))
        if points:
            return points
    return []


def _curve_points_from_values(values) -> list[tuple[float, float]]:
    if not isinstance(values, list):
        return []
    points = []
    for value in values:
        point = _curve_point_from_value(value)
        if point is not None:
            points.append(point)
    return sorted(points, key=lambda point: (point[0], point[1]))


def _base_curve_points_from_values(values) -> list[tuple[float, float]]:
    if not isinstance(values, list):
        return []
    points = []
    for value in values:
        point = _base_curve_point_from_value(value)
        if point is not None:
            points.append(point)
    return sorted(points, key=lambda point: (point[0], point[1]))


def _curve_plan_from_values(values) -> list[dict]:
    if not isinstance(values, list):
        return []
    plan = []
    for value in values:
        if not isinstance(value, dict):
            continue
        try:
            item = {
                "index": int(value["index"]),
                "voltage_mv": int(round(float(value["voltage_mv"]))),
                "base_mhz": int(round(float(value["base_mhz"]))),
                "target_mhz": int(round(float(value["target_mhz"]))),
            }
        except (KeyError, TypeError, ValueError):
            continue
        try:
            item["new_offset_mhz"] = int(
                value.get("new_offset_mhz", item["target_mhz"] - item["base_mhz"])
            )
        except (TypeError, ValueError):
            item["new_offset_mhz"] = item["target_mhz"] - item["base_mhz"]
        plan.append(item)
    return sorted(plan, key=lambda item: (item["voltage_mv"], item["index"]))


def _curve_point_from_value(value) -> tuple[float, float] | None:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        voltage = value[0]
        clock = value[1]
    elif isinstance(value, dict):
        voltage = _curve_point_value(value, "voltage_mv", "voltage", "mv", "x")
        clock = _curve_point_value(
            value,
            "clock_mhz",
            "target_mhz",
            "frequency_mhz",
            "base_mhz",
            "mhz",
            "y",
        )
    else:
        return None
    try:
        voltage_value = float(voltage)
        clock_value = float(clock)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(voltage_value) or not math.isfinite(clock_value):
        return None
    return voltage_value, clock_value


def _nearest_profile_curve_point(
    x_value: float,
    y_value: float,
    points: list[tuple[float, float]],
    view_range,
    *,
    max_normalized_distance: float = 0.04,
) -> tuple[float, float] | None:
    if not points:
        return None
    try:
        (x_min, x_max), (y_min, y_max) = view_range
        x_span = max(1.0, float(x_max) - float(x_min))
        y_span = max(1.0, float(y_max) - float(y_min))
    except (TypeError, ValueError):
        x_span = 1.0
        y_span = 1.0
    best_point = None
    best_distance = None
    for point in points:
        distance = (
            ((float(point[0]) - float(x_value)) / x_span) ** 2
            + ((float(point[1]) - float(y_value)) / y_span) ** 2
        ) ** 0.5
        if best_distance is None or distance < best_distance:
            best_point = point
            best_distance = distance
    if best_distance is None or best_distance > float(max_normalized_distance):
        return None
    return best_point


def _base_curve_point_from_value(value) -> tuple[float, float] | None:
    if not isinstance(value, dict):
        return None
    voltage = _curve_point_value(value, "voltage_mv", "voltage", "mv", "x")
    clock = _curve_point_value(
        value,
        "base_mhz",
        "base_clock_mhz",
        "clock_mhz",
        "target_mhz",
        "frequency_mhz",
        "mhz",
        "y",
    )
    try:
        voltage_value = float(voltage)
        clock_value = float(clock)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(voltage_value) or not math.isfinite(clock_value):
        return None
    return voltage_value, clock_value


def _curve_point_value(point: dict, *keys: str):
    for key in keys:
        value = point.get(key)
        if value not in (None, ""):
            return value
    return None


def _base_curve_cache_path() -> Path:
    return default_user_config_dir() / "base-vf-curve.json"


def _load_cached_base_curve_points() -> list[tuple[float, float]]:
    path = _base_curve_cache_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    gpu_index = payload.get("gpu_index")
    if gpu_index not in (None, ""):
        try:
            if int(gpu_index) != _runtime_gpu_index(default_runtime_config_path()):
                return []
        except (TypeError, ValueError):
            return []
    return _curve_points_from_values(payload.get("points"))


def _save_cached_base_curve_points(points: list[tuple[float, float]]) -> None:
    normalized = _curve_points_from_values(points)
    if not normalized:
        return
    payload = {
        "format_version": 1,
        "saved_at": datetime.now().astimezone().isoformat(),
        "gpu_index": _runtime_gpu_index(default_runtime_config_path()),
        "points": [
            {"voltage_mv": float(voltage), "clock_mhz": float(clock)}
            for voltage, clock in normalized
        ],
    }
    path = _base_curve_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return


def _profile_curve_target_point(profile: dict) -> tuple[float, float] | None:
    voltage = profile.get("candidate_voltage_mv", profile.get("voltage_mv"))
    clock = profile.get("lock_clock_mhz", profile.get("clock_mhz"))
    try:
        voltage_value = float(voltage)
        clock_value = float(clock)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(voltage_value) or not math.isfinite(clock_value):
        return None
    return voltage_value, clock_value


def _profile_curve_tab_key(profile: dict) -> str:
    if str(profile.get("runtime_source", "")).strip() == "afterburner":
        parts = [
            AFTERBURNER_PROFILE_ID,
            str(profile.get("afterburner_device_profile", "")).strip(),
            str(profile.get("afterburner_profile", "")).strip(),
            str(profile.get("profile_created_at", "")).strip(),
        ]
        return ":".join(part for part in parts if part)
    for key in ("profile_id", "candidate_id", "path", "display_name"):
        value = str(profile.get(key, "")).strip()
        if value:
            return value
    return "profile-curve"


def _profile_curve_tab_label(profile: dict) -> str:
    display_name = str(profile.get("display_name", "")).strip()
    if display_name:
        return display_name
    return profile_display_name(profile) or _profile_curve_tab_key(profile)


def _profile_curve_legend_label(profile: dict) -> str:
    if str(profile.get("runtime_source", "")).strip() == "afterburner":
        return "MSI Afterburner"
    source = str(profile.get("profile_source", "")).strip()
    if source in {"auto-uv-final", "profile-store"}:
        return "Auto-UV"
    return source or "Curve"


def _profile_fan_curve_tab_key(profile: dict) -> str:
    return f"fan:{_profile_curve_tab_key(profile)}"


def _profile_fan_curve_tab_label(profile: dict) -> str:
    base_label = _profile_curve_tab_label(profile)
    return f"{base_label} Fan Curve"


def _profile_fan_curve_legend_label(profile: dict) -> str:
    if str(profile.get("runtime_source", "")).strip() == "afterburner":
        return "MSI Afterburner"
    return "Silent Fan Curve"


def _afterburner_profile_status(section: dict) -> tuple[str, bool]:
    if bool(section.get("is_valid_manual_candidate")):
        return "Ready", True
    if not bool(section.get("is_manual_candidate")):
        return "Not Importable: same as Defaults or Startup", False
    validation = section.get("flatten_validation")
    if isinstance(validation, dict):
        reason = str(validation.get("reason", "")).strip()
        if reason:
            return f"Not Importable: {reason}", False
        description = describe_afterburner_flatten_validation(validation)
        if description:
            return f"Not Importable: {description}", False
    return "Not Importable: invalid Afterburner V/F preset", False


def _relative_profile_path(root: str | Path, profile_path: str | Path) -> str:
    root_path = Path(root).expanduser().resolve()
    path = Path(profile_path).expanduser().resolve()
    try:
        return str(path.relative_to(root_path))
    except ValueError:
        return path.name


def _runtime_gpu_index(config_path: Path) -> int:
    try:
        config = load_config(config_path)
    except Exception:
        return 0
    gpu = config.get("gpu", {}) if isinstance(config, dict) else {}
    try:
        return max(0, int(gpu.get("index", 0)))
    except (AttributeError, TypeError, ValueError):
        return 0


def _memory_offset_mhz_range() -> tuple[int, int]:
    fallback = (0, 2000)
    controller = None
    try:
        from nvml_gpu_policy import NvmlGpuPolicyController

        controller = NvmlGpuPolicyController(
            gpu_index=_runtime_gpu_index(default_runtime_config_path())
        )
        driver_range = controller.get_memory_clock_offset_range_mhz()
    except Exception:
        return fallback
    finally:
        if controller is not None:
            try:
                controller.close()
            except Exception:
                pass
    if not driver_range:
        return fallback
    _driver_min, driver_max = driver_range
    try:
        max_mhz = int(driver_max)
    except (TypeError, ValueError):
        return fallback
    return 0, max(0, min(fallback[1], max_mhz))


def _persist_afterburner_import_selection(entry: dict) -> dict:
    if not bool(entry.get("importable")):
        raise ValueError("selected Afterburner profile is not importable")
    config_path = default_runtime_config_path()
    source_root = resolve_afterburner_root(entry.get("afterburner_root", "")).resolve()
    section = str(entry.get("section", "")).strip()
    if not section:
        raise ValueError("selected Afterburner profile has no section name")
    source_profile_path = Path(str(entry.get("profile_path", ""))).expanduser()
    if not source_profile_path.is_file():
        raise FileNotFoundError(f"Afterburner profile file not found: {source_profile_path}")
    device_profile_relative_path = _relative_profile_path(
        source_root,
        source_profile_path,
    )
    managed_root = sync_afterburner_export_tree(source_root, managed_afterburner_root())
    runtime_options = load_afterburner_runtime_options(config_path)
    runtime_options["afterburner_root"] = str(managed_root)
    runtime_options["afterburner_profile"] = str(section)
    runtime_options["afterburner_device_profile"] = str(device_profile_relative_path)
    persist_afterburner_import(
        config_path,
        _runtime_gpu_index(config_path),
        managed_root,
        device_profile_relative_path,
        section,
        runtime_options=runtime_options,
    )
    return {
        "afterburner_root": str(managed_root),
        "device_profile_relative_path": str(device_profile_relative_path),
        "section": str(section),
        "config_path": str(config_path),
    }


def _afterburner_import_profile_summary() -> dict | None:
    config_path = default_runtime_config_path()
    try:
        options = load_afterburner_runtime_options(config_path)
    except Exception:
        return None
    root = str(options.get("afterburner_root", "")).strip()
    section = str(options.get("afterburner_profile", "")).strip()
    device_profile = str(options.get("afterburner_device_profile", "")).strip()
    if not root or not section:
        return None
    try:
        source = resolve_afterburner_vf_source(
            afterburner_root=root,
            section=section,
            device_profile_hint=device_profile or None,
            dangerously_skip_validation=bool(
                options.get("dangerously_skip_validation")
            ),
        )
    except Exception:
        return None
    section_info = source.get("section_info", {})
    target = section_info.get("flatten_target")
    clock = None
    voltage = None
    if isinstance(target, dict):
        try:
            clock = int(round(float(target.get("lock_clock_mhz"))))
            voltage = int(round(float(target.get("lock_voltage_mv"))))
        except (TypeError, ValueError):
            clock = None
            voltage = None
    label = f"MSI Afterburner {source['section']}"
    if clock is not None and voltage is not None:
        label += f" {clock} MHz {voltage} mV"
    return {
        "profile_id": AFTERBURNER_PROFILE_ID,
        "candidate_id": AFTERBURNER_PROFILE_ID,
        "profile_created_at": _path_mtime_iso(config_path),
        "profile_source": "MSI Afterburner",
        "runtime_source": "afterburner",
        "path": "",
        "display_name": label,
        "candidate_voltage_mv": voltage,
        "lock_clock_mhz": clock,
        "avg_core_clock_mhz": None,
        "avg_fps": None,
        "avg_power_w": None,
        "efficiency_fps_per_w": None,
        "final_verified": False,
        "afterburner_root": str(source["afterburner_root"]),
        "afterburner_device_profile": str(source["device_profile_relative_path"]),
        "afterburner_profile": str(source["section"]),
        "curve_points": _afterburner_section_curve_points(section_info),
    }


def _path_mtime_iso(path: str | Path) -> str:
    try:
        return datetime.fromtimestamp(Path(path).stat().st_mtime).astimezone().isoformat()
    except OSError:
        return ""


def _runner_status_text(
    profiles: list[dict],
    *,
    running_selector: str = "",
    autostart_selector: str = "",
    running_silent_fan: bool = False,
    autostart_silent_fan: bool = False,
) -> str:
    running_selector = str(running_selector or "").strip()
    autostart_selector = str(autostart_selector or "").strip()
    if running_selector:
        autostarts = _profile_selectors_match(
            profiles,
            running_selector,
            autostart_selector,
        )
        parts = [
            f"Currently running profile: {_profile_status_label(profiles, running_selector)}",
            f"Systemd autostart: {'Yes' if autostarts else 'No'}",
            f"Silent fan curve: {_on_off(running_silent_fan)}",
        ]
        if autostart_selector and not autostarts:
            parts.append(
                "Autostart profile: "
                f"{_profile_status_label(profiles, autostart_selector)}"
            )
        return "; ".join(parts) + "."
    if autostart_selector:
        return (
            f"Autostart profile: {_profile_status_label(profiles, autostart_selector)}; "
            "Systemd autostart: Yes; "
            f"Silent fan curve: {_on_off(autostart_silent_fan)}; "
            "Not running now."
        )
    return "No running/autostart profile available yet."


def _profile_status_label(profiles: list[dict], selector: str) -> str:
    profile = _profile_for_selector(profiles, selector)
    if profile is None:
        text = str(selector or "").strip()
        if text == "__systemd_default__":
            return "latest Auto-UV profile"
        return text or "unknown profile"
    display_name = str(profile.get("display_name", "")).strip()
    if display_name:
        return display_name
    text = _profile_frequency_voltage(profile)
    return text or profile_display_name(profile) or str(profile.get("profile_id", ""))


def _profile_frequency_voltage(profile: dict) -> str:
    clock = _status_number(profile.get("lock_clock_mhz"), precision=0)
    voltage = _status_number(profile.get("candidate_voltage_mv"), precision=0)
    if clock and voltage:
        return f"{clock} MHz {voltage} mV"
    if clock:
        return f"{clock} MHz"
    if voltage:
        return f"{voltage} mV"
    return ""


def _final_result_frequency_voltage(payload: dict) -> str:
    clock = _status_number(
        payload.get("clock_mhz", payload.get("lock_clock_mhz")),
        precision=0,
    )
    voltage = _status_number(
        payload.get("voltage_mv", payload.get("candidate_voltage_mv")),
        precision=0,
    )
    if clock and voltage:
        return f"{clock} MHz {voltage} mV"
    if clock:
        return f"{clock} MHz"
    if voltage:
        return f"{voltage} mV"
    return ""


def _final_profile_notice_text(
    profiles: list[dict],
    *,
    profile_id: str = "",
    candidate_id: str = "",
    result_payload: dict | None = None,
) -> str:
    profile = _profile_for_selector(profiles, profile_id) or _profile_for_selector(
        profiles,
        candidate_id,
    )
    label = ""
    if profile is not None:
        label = _profile_frequency_voltage(profile)
    if not label:
        label = _final_result_frequency_voltage(result_payload or {})
    if label:
        return (
            f"Final verification complete. Profile {label} is saved and "
            "highlighted in Profiles."
        )
    return "Final verification complete. The saved profile is highlighted in Profiles."


def _status_number(value, *, precision: int) -> str:
    if value in (None, ""):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if precision <= 0:
        return str(int(round(number)))
    return f"{number:.{int(precision)}f}"


def _profile_for_selector(profiles: list[dict], selector: str) -> dict | None:
    text = str(selector or "").strip()
    if not text:
        return None
    if text == "latest":
        return profiles[0] if profiles else None
    if text == "__systemd_default__":
        return profiles[0] if profiles else None
    for profile in profiles:
        path = str(profile.get("path", "")).strip()
        names = {Path(path).name, Path(path).stem} if path else set()
        if text in {
            str(profile.get("profile_id", "")),
            str(profile.get("candidate_id", "")),
            path,
            *names,
        }:
            return profile
    return None


def _profile_selectors_match(
    profiles: list[dict],
    left_selector: str,
    right_selector: str,
) -> bool:
    left = str(left_selector or "").strip()
    right = str(right_selector or "").strip()
    if not left or not right:
        return False
    if left == right:
        return True
    left_profile = _profile_for_selector(profiles, left)
    right_profile = _profile_for_selector(profiles, right)
    if left_profile is None or right_profile is None:
        return False
    return str(left_profile.get("profile_id", "")) == str(
        right_profile.get("profile_id", "")
    )


def _on_off(value: bool) -> str:
    return "On" if bool(value) else "Off"


def _systemd_auto_uv_profile_selector() -> str:
    return str(_systemd_autostart_profile_info()["selector"])


def _systemd_autostart_profile_selector() -> str:
    return str(_systemd_autostart_profile_info()["selector"])


def _systemd_autostart_profile_info() -> dict[str, object]:
    if not _systemd_service_is_enabled():
        return {"selector": "", "silent_fan_curve": False}
    command = _systemd_unit_exec_start()
    return _profile_info_from_command_text(command, default_if_present=True)


def _running_auto_uv_profile_info() -> dict[str, object]:
    command = _systemd_running_exec_start()
    info = _profile_info_from_command_text(command, default_if_present=True)
    if str(info["selector"]):
        return info
    return _systemd_autostart_profile_info()


def _profile_info_from_command_text(
    command_text: str,
    *,
    default_if_present: bool = False,
) -> dict[str, object]:
    parts = _command_parts(command_text)
    selector = _profile_selector_from_command_parts(parts)
    if not selector and default_if_present and str(command_text).strip():
        selector = "__systemd_default__"
    return {
        "selector": selector,
        "silent_fan_curve": "--silent-fan-curve" in parts,
    }


def _profile_selector_from_command_parts(parts: list[str]) -> str:
    if "--prefer-afterburner-curve" in parts:
        return AFTERBURNER_PROFILE_ID
    for index, part in enumerate(parts):
        if part == "--auto-uv-profile" and index + 1 < len(parts):
            return str(parts[index + 1])
        if part.startswith("--auto-uv-profile="):
            return part.split("=", 1)[1]
    return ""


def _command_parts(command_text: str) -> list[str]:
    try:
        return shlex.split(str(command_text or ""))
    except ValueError:
        return str(command_text or "").split()


def _systemd_unit_exec_start() -> str:
    path = systemd_service_unit_path()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    service_exists = bool(text.strip())
    for line in text.splitlines():
        if not line.startswith("ExecStart="):
            continue
        return line.split("=", 1)[1]
    return "__systemd_default__" if service_exists else ""


def _systemd_unit_entry_exists() -> bool:
    try:
        return systemd_service_unit_path().is_file()
    except OSError:
        return False


def _systemd_service_is_enabled() -> bool:
    unit_name = f"{PENGUIN_BURNER_UNIT_NAME}.service"
    try:
        result = subprocess.run(
            [SYSTEMCTL, "is-enabled", "--quiet", unit_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=1.5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return int(result.returncode) == 0


def _systemd_running_exec_start() -> str:
    unit_name = f"{PENGUIN_BURNER_UNIT_NAME}.service"
    try:
        result = subprocess.run(
            [SYSTEMCTL, "show", unit_name, "--property=ExecStart", "--value"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=1.5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if int(result.returncode) != 0:
        return ""
    return result.stdout.strip()


def _penguin_burner_runtime_is_active() -> bool:
    unit_name = f"{PENGUIN_BURNER_UNIT_NAME}.service"
    try:
        result = subprocess.run(
            [SYSTEMCTL, "is-active", "--quiet", unit_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=1.5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return int(result.returncode) == 0


def _fan_curve_points_from_payload(payload: dict) -> list[tuple[float, float]]:
    if not isinstance(payload, dict):
        return []
    fan = payload.get("fan")
    if isinstance(fan, dict):
        for key in ("curve", "points"):
            points = _fan_curve_points_from_values(fan.get(key))
            if points:
                return points
    for key in ("curve", "points", "fan_points"):
        points = _fan_curve_points_from_values(payload.get(key))
        if points:
            return points
    return []


def _saved_auto_uv_fan_curve_payload() -> dict | None:
    return _read_json_file(_auto_uv_fan_curve_payload_path())


def _auto_uv_fan_curve_payload_path() -> Path:
    return default_user_config_dir() / "auto-uv-fan-curve.json"


def _write_auto_uv_fan_curve_payload(payload: dict) -> Path:
    if not isinstance(payload, dict):
        raise ValueError("fan curve payload must be an object")
    path = _auto_uv_fan_curve_payload_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    claim_desktop_user_ownership(path.parent, include_parents=True)
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(path)
    claim_desktop_user_ownership(path)
    return path


def _fan_payload_has_silent_runtime_fields(payload: dict) -> bool:
    if not isinstance(payload, dict):
        return False
    if not _fan_curve_points_from_payload(payload):
        return False
    return payload.get("loaded_temperature_c") not in (None, "")


def _fan_curve_target_point_from_payload(payload: dict) -> tuple[float, float] | None:
    if not isinstance(payload, dict):
        return None
    for temp_key, speed_key in (
        ("load_anchor_temperature_c", "load_anchor_fan_speed_pct"),
        ("loaded_temperature_c", "observed_fan_speed_pct"),
    ):
        point = _fan_point_from_values(payload.get(temp_key), payload.get(speed_key))
        if point is not None:
            return point
    telemetry = payload.get("telemetry")
    final = telemetry.get("final") if isinstance(telemetry, dict) else None
    if isinstance(final, dict):
        for temp_key in ("max_temperature_c", "avg_temperature_c"):
            for speed_key in ("max_fan_speed_pct", "avg_fan_speed_pct"):
                point = _fan_point_from_values(final.get(temp_key), final.get(speed_key))
                if point is not None:
                    return point
    points = _fan_curve_points_from_payload(payload)
    return points[-1] if points else None


def _fan_measurement_points_from_payload(payload: dict) -> list[tuple[float, float]]:
    if not isinstance(payload, dict):
        return []
    telemetry = payload.get("telemetry")
    if isinstance(telemetry, dict):
        for key in ("measured_fan_points", "measured_points", "probe_points"):
            points = _fan_measurement_points(telemetry.get(key))
            if points:
                return _sorted_unique_fan_points(points)
    for key in ("measured_fan_points", "measured_points", "probe_points"):
        points = _fan_measurement_points(payload.get(key))
        if points:
            return _sorted_unique_fan_points(points)
    return []


def _fan_curve_points_from_values(values) -> list[tuple[float, float]]:
    if not isinstance(values, list):
        return []
    points = []
    for value in values:
        point = None
        if isinstance(value, dict):
            point = _fan_point_from_values(
                value.get(
                    "temperature_c",
                    value.get("temp_c", value.get("temperature", value.get("x"))),
                ),
                value.get(
                    "speed_pct",
                    value.get(
                        "fan_speed_pct",
                        value.get("fan_pct", value.get("speed", value.get("y"))),
                    ),
                ),
            )
        elif isinstance(value, (list, tuple)) and len(value) >= 2:
            point = _fan_point_from_values(value[0], value[1])
        if point is not None:
            points.append(point)
    return _sorted_unique_fan_points(points)


def _fan_measurement_point(payload: dict) -> tuple[float, float] | None:
    return _fan_point_from_values(
        payload.get(
            "temp_c",
            payload.get("temperature_c", payload.get("avg_temperature_c")),
        ),
        payload.get(
            "fan_pct",
            payload.get("fan_speed_pct", payload.get("avg_fan_speed_pct")),
        ),
    )


def _fan_measurement_points(values) -> list[tuple[float, float]]:
    points = []
    if not isinstance(values, list):
        return points
    for value in values:
        point = None
        if isinstance(value, dict):
            point = _fan_measurement_point(value)
        elif isinstance(value, (list, tuple)) and len(value) >= 2:
            point = _fan_point_from_values(value[0], value[1])
        if point is not None:
            points.append(point)
    return points


def _sorted_unique_fan_points(
    points: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    unique: dict[tuple[float, float], tuple[float, float]] = {}
    for point in points:
        normalized = _fan_point_from_values(point[0], point[1])
        if normalized is None:
            continue
        key = (round(normalized[0], 2), round(normalized[1], 2))
        unique[key] = key
    return sorted(unique.values(), key=lambda point: (point[0], point[1]))


def _fan_point_from_values(temp, fan) -> tuple[float, float] | None:
    try:
        temp_value = float(temp)
        fan_value = float(fan)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(temp_value) or not math.isfinite(fan_value):
        return None
    if not (0.0 <= fan_value <= 100.0):
        return None
    return (temp_value, fan_value)


def _fan_points(payload: dict) -> list[tuple[float, float]]:
    return _fan_curve_points_from_payload(payload)


def _stage_title(value) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"base-baseline", "stock-baseline"}:
        return "Baseline"
    text = raw.replace("-", " ")
    if "candidate" in text:
        return "Undervolting Candidates Sweep"
    return text.title() if text else "Probe"


def _stop_request_path() -> Path:
    return default_user_config_dir() / "auto-uv-stop-requested"


def _reverify_stop_request_path() -> Path:
    return default_user_config_dir() / "profile-reverify-stop-requested"


def _runtime_action_dialog_label(action: str) -> str:
    labels = {
        "daemonize": "Apply selected profile",
        "install-systemd": "Apply selected profile with autostart",
        "uninstall-systemd": "Remove autostart entry",
        "reverify": "Profile verification",
    }
    return labels.get(str(action or "").strip(), "Runtime profile action")


def _error_dialog_copy_text(
    title: str,
    message: str,
    *,
    details: str = "",
) -> str:
    parts = [str(title or "").strip(), "", str(message or "").strip()]
    details = str(details or "").strip()
    if details:
        parts.extend(["", details])
    return "\n".join(part for part in parts if part or part == "")


def _process_failure_details(
    *,
    action_label: str,
    exit_code,
    exit_status: str,
    extra_details: str = "",
    log_tail: str = "",
) -> str:
    lines = [
        f"Action: {str(action_label or '').strip() or 'Unknown action'}",
        f"Exit code: {exit_code}",
        f"Exit status: {str(exit_status or '').strip() or 'Unknown'}",
    ]
    extra_details = str(extra_details or "").strip()
    if extra_details:
        lines.extend(["", extra_details])
    log_tail = str(log_tail or "").strip()
    if log_tail:
        lines.extend(["", "Recent logs:", log_tail])
    return "\n".join(lines)


def _qt_enum_name(value) -> str:
    name = getattr(value, "name", None)
    if name:
        return str(name)
    text = str(value or "").strip()
    if "." in text:
        return text.rsplit(".", 1)[-1]
    return text


def _qt_flags(parent, enum_name: str, *names: str):
    enum = getattr(parent, enum_name, parent)
    value = None
    for name in names:
        flag = getattr(enum, name)
        value = flag if value is None else value | flag
    return value


def _selectable_text_flags(QtCore):
    return _qt_flags(
        QtCore.Qt,
        "TextInteractionFlag",
        "TextSelectableByMouse",
        "TextSelectableByKeyboard",
    )


def _critical_error_icon(QtGui, QtWidgets, widget):
    icon = QtGui.QIcon.fromTheme("dialog-error")
    if not icon.isNull():
        return icon
    standard_pixmap = getattr(QtWidgets.QStyle, "StandardPixmap", QtWidgets.QStyle)
    return widget.style().standardIcon(
        getattr(standard_pixmap, "SP_MessageBoxCritical")
    )


def _fixed_width_font(QtGui):
    font_database_enum = getattr(
        QtGui.QFontDatabase,
        "SystemFont",
        QtGui.QFontDatabase,
    )
    font = QtGui.QFontDatabase.systemFont(getattr(font_database_enum, "FixedFont"))
    style_hint_enum = getattr(QtGui.QFont, "StyleHint", QtGui.QFont)
    font.setStyleHint(getattr(style_hint_enum, "Monospace"))
    font.setFixedPitch(True)
    point_size = font.pointSize()
    font.setPointSize(max(8, min(point_size if point_size > 0 else 9, 9)))
    return font


def _plain_text_no_wrap_mode(QtWidgets):
    line_wrap_enum = getattr(
        QtWidgets.QPlainTextEdit,
        "LineWrapMode",
        QtWidgets.QPlainTextEdit,
    )
    return getattr(line_wrap_enum, "NoWrap")


def _apply_dark_palette(app, QtGui) -> None:
    palette = QtGui.QPalette(app.palette())
    roles = QtGui.QPalette
    colors = {
        roles.Window: "#111418",
        roles.WindowText: "#d8dee9",
        roles.Base: "#171b21",
        roles.AlternateBase: "#1b2027",
        roles.ToolTipBase: "#1f242c",
        roles.ToolTipText: "#f2f5f2",
        roles.Text: "#d8dee9",
        roles.Button: "#252a31",
        roles.ButtonText: "#f2f5f2",
        roles.BrightText: "#ff6b6b",
        roles.Highlight: "#2f6f55",
        roles.HighlightedText: "#ffffff",
        roles.Link: "#7fb4ff",
    }
    for role, color in colors.items():
        palette.setColor(role, QtGui.QColor(color))
    palette.setColor(roles.Disabled, roles.Text, QtGui.QColor("#7f8794"))
    palette.setColor(roles.Disabled, roles.ButtonText, QtGui.QColor("#7f8794"))
    palette.setColor(roles.Disabled, roles.WindowText, QtGui.QColor("#7f8794"))
    app.setPalette(palette)


def _application_icon(QtGui):
    icon = QtGui.QIcon.fromTheme(APP_ICON_NAME)
    if not icon.isNull():
        return icon
    icon_path = _application_icon_path()
    if icon_path is None:
        return QtGui.QIcon()
    return QtGui.QIcon(str(icon_path))


def _application_icon_path() -> Path | None:
    return _asset_image_path(f"{APP_ICON_NAME}.png")


def _asset_image_path(filename: str) -> Path | None:
    try:
        return Path(
            str(
                files("penguin_burner_ui").joinpath(
                    "assets",
                    filename,
                )
            )
        )
    except Exception:
        return None


def _application_version() -> str:
    try:
        return package_version("penguin-burner")
    except PackageNotFoundError:
        pass
    try:
        pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
        text = pyproject_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "development"
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, flags=re.MULTILINE)
    return match.group(1) if match else "development"


def _parse_gui_args(argv: list[str] | None = None) -> tuple[list[str], bool]:
    raw = list(sys.argv if argv is None else argv)
    if not raw:
        raw = ["penguin-burner-ui"]
    qt_argv = [raw[0]]
    yolo = False
    for arg in raw[1:]:
        if arg == "--yolo":
            yolo = True
            continue
        qt_argv.append(arg)
    return qt_argv, bool(yolo)


def _run_gui(argv: list[str] | None = None) -> int:
    qt_argv, yolo = _parse_gui_args(sys.argv if argv is None else argv)
    try:
        qt_modules = _import_qt()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    _QtCore, _QtGui, QtWidgets, _pg = qt_modules
    app = QtWidgets.QApplication(qt_argv)
    app.setApplicationName(APP_DISPLAY_NAME)
    if hasattr(app, "setApplicationDisplayName"):
        app.setApplicationDisplayName(APP_DISPLAY_NAME)
    if hasattr(app, "setDesktopFileName"):
        app.setDesktopFileName(APP_DESKTOP_ID)
    icon = _application_icon(_QtGui)
    if not icon.isNull():
        app.setWindowIcon(icon)
    _apply_dark_palette(app, _QtGui)
    window = AutoUvWindow(qt_modules, yolo=bool(yolo))
    window.show()
    return int(app.exec())


def main() -> int:
    return _run_gui(sys.argv)


def main_yolo() -> int:
    argv = list(sys.argv)
    if "--yolo" not in argv[1:]:
        argv.append("--yolo")
    return _run_gui(argv)


STYLESHEET = """
QMainWindow {
    background: #111418;
    color: #d8dee9;
    font-size: 12px;
}
QLabel#stageLabel {
    color: #8fbc8f;
    font-size: 22px;
    font-weight: 700;
}
QLabel#candidateLabel {
    color: #e5c07b;
    font-size: 13px;
    font-weight: 400;
}
QLineEdit, QPlainTextEdit, QTableWidget {
    background: #171b21;
    border: 1px solid #2e3440;
    color: #d8dee9;
    selection-background-color: #2f6f55;
}
QGroupBox {
    border: 1px solid #2e3440;
    margin-top: 18px;
    padding-top: 14px;
}
QGroupBox::title {
    color: #e5c07b;
    left: 10px;
    padding: 0 6px;
}
QHeaderView::section {
    background: #1f242c;
    color: #d8dee9;
    border: 0;
    font-weight: 400;
    padding: 5px;
}
QPushButton {
    background: #2d5f48;
    border: 1px solid #3d8060;
    border-radius: 4px;
    color: #f2f5f2;
    font-weight: 700;
    padding: 7px 12px;
}
QPushButton:hover {
    border-color: #72d596;
}
QPushButton:pressed {
    background: #244d3b;
    border-color: #8be3a8;
    padding-top: 8px;
    padding-bottom: 6px;
}
QPushButton:disabled {
    background: #252a31;
    border-color: #333944;
    color: #7f8794;
}
QPushButton:checked {
    background: #c4772a;
    border-color: #e1a45d;
    color: #111418;
}
QPushButton#startAutoUvButton {
    background: #c4772a;
    border-color: #e1a45d;
    color: #fff7ec;
}
QPushButton#startAutoUvButton:hover {
    border-color: #ffc57a;
}
QPushButton#startAutoUvButton:pressed {
    background: #9f5f21;
    border-color: #ffd19a;
}
QPushButton#stopButton {
    background: #a73535;
    border-color: #d45d5d;
    color: #fff4f4;
}
QPushButton#stopButton:hover {
    border-color: #ff8989;
}
QPushButton#stopButton:pressed {
    background: #7f2525;
    border-color: #ff9e9e;
}
QPushButton#importAfterburnerButton {
    background: #2d5f48;
    border-color: #3d8060;
    color: #f2f5f2;
}
QPushButton#importAfterburnerButton:hover {
    border-color: #72d596;
}
QPushButton#importAfterburnerButton:pressed {
    background: #244d3b;
    border-color: #8be3a8;
}
QPushButton#aboutButton {
    background: #29313b;
    border-color: #465568;
    color: #f2f5f2;
}
QPushButton#aboutButton:hover {
    border-color: #7f93ad;
}
QPushButton#aboutButton:pressed {
    background: #1f2630;
    border-color: #9fb1c7;
}
QPushButton#startAutoUvButton:disabled,
QPushButton#stopButton:disabled,
QPushButton#importAfterburnerButton:disabled {
    background: #252a31;
    border-color: #333944;
    color: #7f8794;
}
QPushButton#discardFinalChoiceButton {
    background: #a73535;
    border-color: #d45d5d;
    color: #fff4f4;
}
QPushButton#discardFinalChoiceButton:hover {
    border-color: #ff8989;
}
QPushButton#discardFinalChoiceButton:pressed {
    background: #7f2525;
    border-color: #ff9e9e;
}
QLabel#aboutTitle {
    color: #f2f5f2;
    font-size: 20px;
    font-weight: 700;
}
QLabel#aboutVersion {
    color: #aab3c1;
    font-size: 12px;
}
QLabel#purposeText {
    color: #d8dee9;
    font-size: 12px;
    font-weight: 400;
    line-height: 1.25;
}
QCheckBox {
    color: #d8dee9;
    font-weight: 700;
    spacing: 8px;
    padding: 5px 6px;
}
QCheckBox:disabled {
    color: #7f8794;
}
QCheckBox::indicator {
    width: 34px;
    height: 18px;
    border-radius: 9px;
    border: 1px solid #3b4451;
    background: #252a31;
}
QCheckBox::indicator:unchecked {
    background: #252a31;
    border-color: #3b4451;
}
QCheckBox::indicator:checked {
    background: #8fbc8f;
    border-color: #c7f6c7;
}
QCheckBox::indicator:disabled {
    background: #1b2027;
    border-color: #333944;
}
QToolButton {
    background: #1f242c;
    border: 1px solid #3b4451;
    border-radius: 4px;
    color: #d8dee9;
    font-weight: 700;
    padding: 3px 8px;
}
QToolButton:hover {
    background: #2a313b;
}
QToolButton:pressed {
    background: #364154;
}
QToolButton#deleteProfilesButton {
    background: #7f2525;
    border-color: #d45d5d;
    color: #fff4f4;
}
QToolButton#deleteProfilesButton:hover {
    background: #963030;
    border-color: #ff8989;
}
QToolButton#deleteProfilesButton:pressed {
    background: #631b1b;
    border-color: #ff9e9e;
}
QToolButton#infoButton {
    background: #252a31;
    border: 1px solid #5b6675;
    border-radius: 9px;
    color: #e5c07b;
    font-size: 11px;
    font-weight: 800;
    padding: 0;
}
QToolButton#infoButton:hover {
    background: #2f3844;
    border-color: #e5c07b;
    color: #fff2c7;
}
QProgressBar#dependencyProgress {
    background: #111418;
    border: 1px solid #3b4451;
    border-radius: 4px;
    color: #f2f5f2;
    font-size: 11px;
    font-weight: 800;
    text-align: center;
}
QProgressBar#dependencyProgress::chunk {
    background: #2f8dd6;
    border-radius: 3px;
}
QTabWidget::pane {
    border: 1px solid #2e3440;
}
QTabBar::tab {
    background: #1b2027;
    color: #c7ced8;
    padding: 8px 12px;
}
QTabBar::tab:selected {
    background: #252b34;
    color: #ffffff;
}
"""
