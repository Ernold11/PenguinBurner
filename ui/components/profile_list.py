from __future__ import annotations

from datetime import datetime
from pathlib import Path

from saved_uv_profiles import profile_display_name
from .. import theme
from .table_sizing import set_header_fit_column_widths


GOOD_DELTA_COLOR = theme.GOOD
BAD_DELTA_COLOR = theme.ERROR
PROFILE_SORT_ROLE = 261
PROFILE_SORTABLE_COLUMNS = frozenset({0, 2, 3, 4, 5, 6, 7})


class ProfileList:
    COLUMNS = [
        "Date",
        "Profile",
        "mV",
        "Target MHz",
        "Effective MHz",
        "FPS/W",
        "FPS",
        "Power W",
        "Mem",
        "Source",
        "Autostart",
    ]
    DATE_COLUMN = 0
    PROFILE_COLUMN = 1
    VOLTAGE_COLUMN = 2
    TARGET_MHZ_COLUMN = 3
    EFFECTIVE_MHZ_COLUMN = 4
    FPSW_COLUMN = 5
    FPS_COLUMN = 6
    POWER_COLUMN = 7
    MEMORY_OFFSET_COLUMN = 8
    SOURCE_COLUMN = 9
    AUTOSTART_COLUMN = 10
    PROFILE_ID_ROLE = 257
    CANDIDATE_ID_ROLE = 258
    PROFILE_PATH_ROLE = 259
    PROFILE_DELETABLE_ROLE = 260
    PROFILE_APPLY_ROLE = 262
    SORT_VALUE_ROLE = PROFILE_SORT_ROLE
    SORTABLE_COLUMNS = PROFILE_SORTABLE_COLUMNS

    def __init__(self, *, QtCore, QtGui, QtWidgets):
        self.QtCore = QtCore
        self.QtGui = QtGui
        self.QtWidgets = QtWidgets
        self._item_class = _sortable_item_class(QtWidgets, self.SORT_VALUE_ROLE)
        self._runtime_actions_available = True
        self._has_systemd_service = False
        self._systemd_selector = ""
        self._syncing_persist_toggle = False
        self._sort_column = self.DATE_COLUMN
        self._sort_order = QtCore.Qt.DescendingOrder
        self.widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(self.widget)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        top = QtWidgets.QHBoxLayout()
        self.silent_fan_checkbox = QtWidgets.QCheckBox("Silent fan curve")
        self.install_button = QtWidgets.QCheckBox("Persist on Startup")
        self.daemonize_button = QtWidgets.QPushButton("Apply Selected")
        self.delete_button = QtWidgets.QToolButton()
        self.delete_button.setObjectName("deleteProfilesButton")
        self.delete_button.setIcon(_standard_trash_icon(QtWidgets, self.widget))
        self.delete_button.setIconSize(QtCore.QSize(18, 18))
        self.delete_button.setToolTip("Delete Selected Profiles")
        self.delete_button.setAccessibleName("Delete Selected Profiles")
        self.remove_button = QtWidgets.QPushButton("Remove Autostart Entry")
        top.addWidget(QtWidgets.QLabel("Stored undervolt profiles"))
        top.addStretch(1)
        top.addWidget(self.silent_fan_checkbox)
        top.addWidget(self.install_button)
        top.addWidget(self.daemonize_button)
        top.addWidget(self.delete_button)
        top.addWidget(self.remove_button)

        self.table = QtWidgets.QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        set_header_fit_column_widths(
            self.table,
            {
                0: 154,
                1: 150,
                self.VOLTAGE_COLUMN: 124,
                self.TARGET_MHZ_COLUMN: 108,
                self.MEMORY_OFFSET_COLUMN: 104,
                self.EFFECTIVE_MHZ_COLUMN: 158,
                self.FPSW_COLUMN: 134,
                self.FPS_COLUMN: 134,
                self.POWER_COLUMN: 144,
                self.SOURCE_COLUMN: 170,
                self.AUTOSTART_COLUMN: 96,
            },
            QtCore=QtCore,
            padding=34,
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        header = self.table.horizontalHeader()
        header.setHighlightSections(False)
        header_font = header.font()
        header_font.setBold(False)
        header.setFont(header_font)
        header.setSectionsClickable(True)
        self._apply_sort_indicator(self._sort_column, self._sort_order)
        header.sectionClicked.connect(self._sort_by_header_column)

        layout.addLayout(top)
        layout.addWidget(self.table, 1)
        self.table.itemSelectionChanged.connect(self._sync_action_state)
        self._sync_action_state()

    def set_profiles(
        self,
        profiles: list[dict],
        *,
        systemd_selector: str = "",
        has_systemd_entry: bool | None = None,
        preferred_candidate_id: str = "",
        preferred_profile_id: str = "",
        select_preferred: bool = False,
        preserve_persist_toggle: bool = True,
        silent_fan_checked: bool | None = None,
        preserve_silent_fan_toggle: bool = True,
    ) -> None:
        selected_profile_ids = self.selected_profile_ids()
        persist_toggle_checked = self.install_button.isChecked()
        silent_fan_checked_before = self.silent_fan_checkbox.isChecked()
        sort_column = self._active_sort_column()
        sort_order = self._sort_order
        profiles = _promote_preferred_profile(
            profiles,
            preferred_candidate_id=preferred_candidate_id,
            preferred_profile_id=preferred_profile_id,
        )
        self._has_systemd_service = (
            bool(has_systemd_entry)
            if has_systemd_entry is not None
            else bool(str(systemd_selector).strip())
        )
        self._systemd_selector = str(systemd_selector or "").strip()
        table_signals_blocked = self.table.blockSignals(True)
        try:
            self.table.setRowCount(0)
            for profile in profiles:
                row = self.table.rowCount()
                self.table.insertRow(row)
                profile_id = str(profile.get("profile_id", ""))
                candidate_id = str(profile.get("candidate_id", ""))
                profile_path = str(profile.get("path", ""))
                is_deletable = bool(profile_path) or str(
                    profile.get("runtime_source", "")
                ) == "afterburner"
                is_applicable = bool(profile.get("final_verified", False)) or str(
                    profile.get("runtime_source", "")
                ) == "afterburner"
                is_preferred = row == 0 and _profile_matches_preference(
                    profile,
                    preferred_candidate_id=preferred_candidate_id,
                    preferred_profile_id=preferred_profile_id,
                )
                values = [
                    _date_text(profile),
                    _profile_name(profile),
                    _format_profile_metric_with_delta(
                        profile.get("candidate_voltage_mv"),
                        _profile_base_metric(profile, "candidate_voltage_mv"),
                        precision=0,
                        lower_is_better=True,
                    ),
                    _format_number(profile.get("lock_clock_mhz"), precision=0),
                    _format_profile_metric_with_delta(
                        profile.get("avg_core_clock_mhz"),
                        _profile_base_metric(profile, "avg_core_clock_mhz"),
                        precision=2,
                    ),
                    _format_profile_metric_with_delta(
                        profile.get("efficiency_fps_per_w"),
                        _profile_base_metric(profile, "efficiency_fps_per_w"),
                        precision=2,
                    ),
                    _format_profile_metric_with_delta(
                        profile.get("avg_fps"),
                        _profile_base_metric(profile, "avg_fps"),
                        precision=2,
                    ),
                    _format_profile_metric_with_delta(
                        profile.get("avg_power_w"),
                        _profile_base_metric(profile, "avg_power_w"),
                        precision=2,
                        lower_is_better=True,
                    ),
                    _format_signed_number(
                        profile.get("memory_offset_mhz"),
                        precision=0,
                    ),
                    _profile_source_label(profile),
                    (
                        "installed"
                        if _profile_matches_selector(profile, systemd_selector)
                        else ""
                    ),
                ]
                sort_values = _profile_sort_values(profile)
                for column, value in enumerate(values):
                    item = self._item_class(str(value))
                    item.setData(self.PROFILE_ID_ROLE, profile_id)
                    item.setData(self.CANDIDATE_ID_ROLE, candidate_id)
                    item.setData(self.PROFILE_PATH_ROLE, profile_path)
                    item.setData(self.PROFILE_DELETABLE_ROLE, bool(is_deletable))
                    item.setData(self.PROFILE_APPLY_ROLE, bool(is_applicable))
                    item.setData(self.SORT_VALUE_ROLE, sort_values[column])
                    if (
                        column in self.SORTABLE_COLUMNS - {self.DATE_COLUMN}
                        or column == self.MEMORY_OFFSET_COLUMN
                    ):
                        item.setTextAlignment(
                            self.QtCore.Qt.AlignRight | self.QtCore.Qt.AlignVCenter
                        )
                    if column == self.VOLTAGE_COLUMN:
                        _paint_profile_delta_item(
                            item,
                            self.QtGui,
                            profile.get("candidate_voltage_mv"),
                            _profile_base_metric(profile, "candidate_voltage_mv"),
                            label="mV",
                            lower_is_better=True,
                        )
                    if column == self.EFFECTIVE_MHZ_COLUMN:
                        _paint_profile_delta_item(
                            item,
                            self.QtGui,
                            profile.get("avg_core_clock_mhz"),
                            _profile_base_metric(profile, "avg_core_clock_mhz"),
                            label="Effective MHz",
                        )
                    if column == self.FPSW_COLUMN:
                        _paint_profile_delta_item(
                            item,
                            self.QtGui,
                            profile.get("efficiency_fps_per_w"),
                            _profile_base_metric(profile, "efficiency_fps_per_w"),
                            label="FPS/W",
                        )
                    if column == self.FPS_COLUMN:
                        _paint_profile_delta_item(
                            item,
                            self.QtGui,
                            profile.get("avg_fps"),
                            _profile_base_metric(profile, "avg_fps"),
                            label="FPS",
                        )
                    if column == self.POWER_COLUMN:
                        _paint_profile_delta_item(
                            item,
                            self.QtGui,
                            profile.get("avg_power_w"),
                            _profile_base_metric(profile, "avg_power_w"),
                            label="Power W",
                            lower_is_better=True,
                        )
                    if column == self.AUTOSTART_COLUMN and value:
                        item.setForeground(
                            self.table.palette().highlightedText().color()
                        )
                    if is_preferred:
                        item.setBackground(self.QtGui.QColor(theme.PROFILE_SELECTED_BG))
                    self.table.setItem(row, column, item)
            self._sort_table(sort_column, sort_order)
            should_preserve_selection = _should_preserve_selection(
                selected_profile_ids,
                preferred_candidate_id=preferred_candidate_id,
                preferred_profile_id=preferred_profile_id,
            )
            if (
                should_preserve_selection
                and selected_profile_ids
                and self.select_profiles(selected_profile_ids)
            ):
                pass
            elif select_preferred and preferred_profile_id:
                self.select_profile(preferred_profile_id)
            elif (
                select_preferred
                and preferred_candidate_id
                and self.select_candidate(preferred_candidate_id)
            ):
                pass
        finally:
            self.table.blockSignals(table_signals_blocked)
        should_preserve_persist_toggle = preserve_persist_toggle and _should_preserve_persist_toggle(
            selected_profile_ids,
            self.selected_profile_ids(),
        )
        if should_preserve_persist_toggle:
            self._set_persist_toggle_checked(persist_toggle_checked)
        should_preserve_silent_fan_toggle = (
            preserve_silent_fan_toggle
            and _should_preserve_persist_toggle(
                selected_profile_ids,
                self.selected_profile_ids(),
            )
        )
        if should_preserve_silent_fan_toggle:
            self._set_silent_fan_checked(silent_fan_checked_before)
        elif silent_fan_checked is not None:
            self._set_silent_fan_checked(bool(silent_fan_checked))
        self._sync_action_state(sync_persist_toggle=not should_preserve_persist_toggle)

    def _active_sort_column(self) -> int:
        column = int(self._sort_column)
        if column not in self.SORTABLE_COLUMNS:
            return 0
        return column

    def _sort_by_header_column(self, column: int) -> None:
        column = int(column)
        if column not in self.SORTABLE_COLUMNS:
            self._apply_sort_indicator(self._sort_column, self._sort_order)
            return
        if int(self._sort_column) == column:
            order = (
                self.QtCore.Qt.AscendingOrder
                if self._sort_order == self.QtCore.Qt.DescendingOrder
                else self.QtCore.Qt.DescendingOrder
            )
        else:
            order = (
                self.QtCore.Qt.DescendingOrder
                if column == 0
                else self.QtCore.Qt.AscendingOrder
            )
        self._sort_column = column
        self._sort_order = order
        self._apply_sort_indicator(column, order)
        self._sort_table(column, order)
        self._sync_action_state(sync_persist_toggle=False)

    def _apply_sort_indicator(self, column: int, order) -> None:
        header = self.table.horizontalHeader()
        signals_blocked = header.blockSignals(True)
        try:
            header.setSortIndicatorShown(True)
            header.setSortIndicator(int(column), order)
        finally:
            header.blockSignals(signals_blocked)

    def _sort_table(self, column: int, order) -> None:
        column = int(column)
        if column not in self.SORTABLE_COLUMNS:
            return
        self._sort_column = column
        self._sort_order = order
        self._apply_sort_indicator(column, order)
        self._item_class.sort_descending = order == self.QtCore.Qt.DescendingOrder
        self.table.sortItems(column, order)

    def selected_profile_id(self) -> str:
        profile_ids = self.selected_profile_ids()
        if not profile_ids:
            return ""
        return profile_ids[-1]

    def selected_profile_ids(self) -> list[str]:
        return self._selected_row_values(self.PROFILE_ID_ROLE)

    def selected_profile_paths(self) -> list[str]:
        return self._selected_row_values(self.PROFILE_PATH_ROLE)

    def selected_profile_names(self) -> list[str]:
        names = []
        for row in self._selected_rows():
            item = self.table.item(row, 1)
            if item is not None and str(item.text()).strip():
                names.append(str(item.text()).strip())
        return names

    def silent_fan_enabled(self) -> bool:
        return bool(self.silent_fan_checkbox.isChecked())

    def persist_on_startup_enabled(self) -> bool:
        return bool(self.install_button.isChecked())

    def selected_profile_name(self) -> str:
        rows = self._selected_rows()
        if not rows:
            return ""
        row = rows[-1]
        item = self.table.item(row, 1)
        return "" if item is None else str(item.text())

    def selected_profile_is_persisted(self) -> bool:
        rows = self._selected_rows()
        if len(rows) != 1:
            return False
        row = rows[-1]
        item = self.table.item(row, self.AUTOSTART_COLUMN)
        return bool(item is not None and str(item.text()).strip())

    def set_runtime_actions_enabled(self, enabled: bool) -> None:
        self._runtime_actions_available = bool(enabled)
        self._sync_action_state(sync_persist_toggle=False)

    def select_profile(self, profile_id: str) -> None:
        self.select_profiles([profile_id])

    def select_profiles(self, profile_ids: list[str]) -> bool:
        wanted = {str(profile_id) for profile_id in profile_ids if str(profile_id)}
        if not wanted:
            return False
        selection_model = self.table.selectionModel()
        if selection_model is None:
            return False
        selection_model.clearSelection()
        flags = _selection_flags(
            self.QtCore,
            "Select",
            "Rows",
        )
        matched = False
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            profile_id = "" if item is None else str(item.data(self.PROFILE_ID_ROLE) or "")
            if profile_id not in wanted:
                continue
            selection_model.select(self.table.model().index(row, 0), flags)
            matched = True
        return matched

    def select_candidate(self, candidate_id: str) -> bool:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and str(
                item.data(self.CANDIDATE_ID_ROLE) or ""
            ) == str(candidate_id):
                self.table.selectRow(row)
                return True
        return False

    def _sync_action_state(self, *, sync_persist_toggle: bool = True) -> None:
        selected_ids = self.selected_profile_ids()
        has_single_selection = self._runtime_actions_available and len(
            selected_ids
        ) == 1
        selected_rows = self._selected_rows()
        has_apply_selection = (
            has_single_selection
            and len(selected_rows) == 1
            and self._row_is_applicable(selected_rows[0])
        )
        has_delete_selection = (
            self._runtime_actions_available
            and bool(selected_rows)
            and all(self._row_is_deletable(row) for row in selected_rows)
        )
        self.daemonize_button.setEnabled(has_apply_selection)
        self.install_button.setEnabled(has_apply_selection)
        self.delete_button.setEnabled(has_delete_selection)
        if sync_persist_toggle and not self._syncing_persist_toggle:
            self._set_persist_toggle_checked(
                self.selected_profile_is_persisted() if has_single_selection else False
            )
        self.remove_button.setEnabled(
            self._runtime_actions_available and self._has_systemd_service
        )

    def _set_persist_toggle_checked(self, checked: bool) -> None:
        self._syncing_persist_toggle = True
        signals_blocked = self.install_button.blockSignals(True)
        try:
            self.install_button.setChecked(bool(checked))
        finally:
            self.install_button.blockSignals(signals_blocked)
            self._syncing_persist_toggle = False

    def _set_silent_fan_checked(self, checked: bool) -> None:
        signals_blocked = self.silent_fan_checkbox.blockSignals(True)
        try:
            self.silent_fan_checkbox.setChecked(bool(checked))
        finally:
            self.silent_fan_checkbox.blockSignals(signals_blocked)

    def _selected_rows(self) -> list[int]:
        selection_model = self.table.selectionModel()
        if selection_model is None:
            return []
        return sorted({int(index.row()) for index in selection_model.selectedRows()})

    def _selected_row_values(self, role: int) -> list[str]:
        values = []
        seen = set()
        for row in self._selected_rows():
            item = self.table.item(row, 0)
            value = "" if item is None else str(item.data(role) or "").strip()
            if not value or value in seen:
                continue
            values.append(value)
            seen.add(value)
        return values

    def _row_is_deletable(self, row: int) -> bool:
        item = self.table.item(int(row), 0)
        return bool(item is not None and item.data(self.PROFILE_DELETABLE_ROLE))

    def _row_is_applicable(self, row: int) -> bool:
        item = self.table.item(int(row), 0)
        return bool(item is not None and item.data(self.PROFILE_APPLY_ROLE))


def _standard_trash_icon(QtWidgets, widget):
    style = widget.style()
    standard_pixmap = getattr(QtWidgets.QStyle, "StandardPixmap", QtWidgets.QStyle)
    return style.standardIcon(getattr(standard_pixmap, "SP_TrashIcon"))


def _sortable_item_class(QtWidgets, sort_role: int):
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
    number = _to_float(value)
    if number is not None:
        return float(number)
    return str(value).casefold()


def _selection_flags(QtCore, *names: str):
    enum = getattr(QtCore.QItemSelectionModel, "SelectionFlag", QtCore.QItemSelectionModel)
    value = None
    for name in names:
        flag = getattr(enum, name)
        value = flag if value is None else value | flag
    return value


def _should_preserve_selection(
    selected_profile_ids: list[str],
    *,
    preferred_candidate_id: str = "",
    preferred_profile_id: str = "",
) -> bool:
    return bool(selected_profile_ids)


def _should_preserve_persist_toggle(
    previous_profile_ids: list[str],
    current_profile_ids: list[str],
) -> bool:
    return (
        len(previous_profile_ids) == 1
        and len(current_profile_ids) == 1
        and list(previous_profile_ids) == list(current_profile_ids)
    )


def _date_text(profile: dict) -> str:
    value = str(
        profile.get("profile_created_at") or profile.get("verified_at") or ""
    ).strip()
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value[:19].replace("T", " ")


def _format_number(value, *, precision: int) -> str:
    if value in (None, ""):
        return ""
    number = _to_float(value)
    if number is None:
        return ""
    precision = max(0, min(int(precision), 2))
    if precision <= 0:
        return str(int(round(number)))
    return f"{number:.{precision}f}"


def _format_signed_number(value, *, precision: int) -> str:
    text = _format_number(value, precision=precision)
    if not text:
        return ""
    number = _to_float(value)
    if number is None:
        return text
    if abs(number) < 0.5:
        return "0"
    return f"+{text}" if number > 0 else text


def _to_float(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _metric_delta_percent(
    current,
    baseline,
) -> float | None:
    current_number = _to_float(current)
    baseline_number = _to_float(baseline)
    if current_number is None or baseline_number in (None, 0.0):
        return None
    return (
        (current_number - float(baseline_number)) / float(baseline_number)
    ) * 100.0


def _format_profile_metric_with_delta(
    current,
    baseline,
    *,
    precision: int,
    lower_is_better: bool = False,
) -> str:
    value_text = _format_number(current, precision=precision)
    if not value_text:
        return ""
    delta = _metric_delta_percent(
        current,
        baseline,
    )
    if delta is None:
        return value_text
    return f"{value_text} ({delta:+.2f}%)"


def _format_profile_metric_delta(
    current,
    baseline,
    *,
    lower_is_better: bool = False,
) -> str:
    delta = _metric_delta_percent(
        current,
        baseline,
    )
    if delta is None:
        return ""
    if abs(delta) < 0.005:
        return "ref"
    return f"{delta:+.2f}%"


def _profile_metric_delta_color(
    current,
    baseline,
    *,
    lower_is_better: bool = False,
) -> str:
    delta = _metric_delta_percent(
        current,
        baseline,
    )
    if delta is None:
        return ""
    improved = delta < -0.005 if lower_is_better else delta > 0.005
    regressed = delta > 0.005 if lower_is_better else delta < -0.005
    if improved:
        return GOOD_DELTA_COLOR
    if regressed:
        return BAD_DELTA_COLOR
    return ""


def _paint_profile_delta_item(
    item,
    QtGui,
    current,
    baseline,
    *,
    label: str,
    lower_is_better: bool = False,
) -> None:
    color = _profile_metric_delta_color(
        current,
        baseline,
        lower_is_better=lower_is_better,
    )
    delta = _metric_delta_percent(
        current,
        baseline,
    )
    if delta is None:
        return
    if color:
        item.setForeground(QtGui.QColor(color))
    base_text = _format_number(baseline, precision=2)
    if lower_is_better:
        comparison = "lower" if delta < 0.0 else "higher"
        if label == "Power W" and delta < 0.0:
            comparison = "saved"
        item.setToolTip(f"{label} {delta:+.2f}% {comparison} vs base {base_text}")
    else:
        item.setToolTip(f"{label} {delta:+.2f}% vs base {base_text}")


def _profile_base_metric(profile: dict, metric: str):
    lookup = {
        "candidate_voltage_mv": (
            "base_candidate_voltage_mv",
            "baseline_candidate_voltage_mv",
            "baseline_voltage_mv",
            "base_voltage_mv",
        ),
        "avg_core_clock_mhz": (
            "base_avg_core_clock_mhz",
            "baseline_avg_core_clock_mhz",
            "baseline_core_clock_mhz",
        ),
        "efficiency_fps_per_w": (
            "base_efficiency_fps_per_w",
            "baseline_efficiency_fps_per_w",
            "baseline_fps_per_w",
        ),
        "avg_power_w": (
            "base_avg_power_w",
            "baseline_avg_power_w",
            "baseline_power_w",
        ),
        "avg_fps": (
            "base_avg_fps",
            "baseline_avg_fps",
            "baseline_fps",
        ),
    }
    for key in lookup.get(str(metric), ()):
        value = profile.get(key)
        if _to_float(value) is not None:
            return value
    return None


def _date_sort_value(profile: dict) -> float:
    value = str(
        profile.get("profile_created_at") or profile.get("verified_at") or ""
    ).strip()
    if not value:
        return 0.0
    try:
        return float(datetime.fromisoformat(value).timestamp())
    except ValueError:
        return 0.0


def _profile_delta_sort_value(
    profile: dict,
    metric: str,
    *,
    lower_is_better: bool = False,
) -> float | str:
    delta = _metric_delta_percent(
        profile.get(metric),
        _profile_base_metric(profile, metric),
    )
    return "" if delta is None else float(delta)


def _profile_value_sort_value(profile: dict, metric: str) -> float | str:
    value = _to_float(profile.get(metric))
    return "" if value is None else float(value)


def _profile_sort_values(profile: dict) -> list[float | str]:
    return [
        _date_sort_value(profile),
        "",
        _profile_value_sort_value(profile, "candidate_voltage_mv"),
        _profile_value_sort_value(profile, "lock_clock_mhz"),
        _profile_value_sort_value(profile, "avg_core_clock_mhz"),
        _profile_value_sort_value(profile, "efficiency_fps_per_w"),
        _profile_value_sort_value(profile, "avg_fps"),
        _profile_value_sort_value(profile, "avg_power_w"),
        "",
        "",
        "",
    ]


def _profile_source_label(profile: dict) -> str:
    source = str(profile.get("profile_source", "")).strip()
    labels = {
        "auto-uv-final": "Auto UV",
        "user-edited": "User edited",
    }
    return labels.get(source, source)


def _profile_name(profile: dict) -> str:
    display_name = str(profile.get("display_name", "")).strip()
    if display_name:
        return display_name
    return profile_display_name(profile)


def _profile_matches_selector(profile: dict, selector: str) -> bool:
    text = str(selector or "").strip()
    if not text:
        return False
    if text == "__systemd_default__":
        return False
    if text == "latest":
        return False
    path = str(profile.get("path", "")).strip()
    names = {Path(path).name, Path(path).stem} if path else set()
    return text in {
        str(profile.get("profile_id", "")),
        str(profile.get("candidate_id", "")),
        path,
        *names,
    }


def _profile_matches_preference(
    profile: dict,
    *,
    preferred_candidate_id: str = "",
    preferred_profile_id: str = "",
) -> bool:
    profile_id = str(profile.get("profile_id", ""))
    candidate_id = str(profile.get("candidate_id", ""))
    if preferred_profile_id and profile_id == str(preferred_profile_id):
        return True
    if preferred_candidate_id and candidate_id == str(preferred_candidate_id):
        return True
    return False


def _promote_preferred_profile(
    profiles: list[dict],
    *,
    preferred_candidate_id: str = "",
    preferred_profile_id: str = "",
) -> list[dict]:
    if not preferred_candidate_id and not preferred_profile_id:
        return list(profiles)
    promoted_index = None
    for index, profile in enumerate(profiles):
        if _profile_matches_preference(
            profile,
            preferred_candidate_id=preferred_candidate_id,
            preferred_profile_id=preferred_profile_id,
        ):
            promoted_index = index
            break
    if promoted_index is None:
        return list(profiles)
    promoted = profiles[promoted_index]
    return [promoted, *profiles[:promoted_index], *profiles[promoted_index + 1 :]]
