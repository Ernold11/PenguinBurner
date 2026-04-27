from __future__ import annotations

from .table_sizing import set_header_fit_column_widths


class RunsTable:
    COLUMNS = [
        "Run",
        "mV",
        "Target MHz",
        "Measured MHz",
        "FPS",
        "FPS vs base",
        "Power W",
        "Power vs base",
        "Temp C",
        "Fan %",
        "FPS/W",
        "FPS/W vs base",
        "OC Budget",
        "Decision",
        "Status",
    ]
    TARGET_MHZ_COLUMN = 2
    MEASURED_MHZ_COLUMN = 3
    FPS_COLUMN = 4
    FPS_DELTA_COLUMN = 5
    POWER_COLUMN = 6
    POWER_DELTA_COLUMN = 7
    TEMP_COLUMN = 8
    FAN_COLUMN = 9
    FPSW_COLUMN = 10
    FPSW_DELTA_COLUMN = 11
    BUDGET_COLUMN = 12
    DECISION_COLUMN = 13
    STATUS_COLUMN = 14

    def __init__(self, *, QtCore, QtGui, QtWidgets):
        self.QtCore = QtCore
        self.QtGui = QtGui
        self.QtWidgets = QtWidgets
        self.base_baseline: dict | None = None
        self._progress_by_probe: dict[tuple[str, str], dict] = {}
        self._busy_progress_class = _busy_progress_class(QtCore, QtGui, QtWidgets)
        self.widget = QtWidgets.QTableWidget(0, len(self.COLUMNS))
        self.widget.setHorizontalHeaderLabels(self.COLUMNS)
        self.widget.horizontalHeader().setStretchLastSection(True)
        self.widget.verticalHeader().setVisible(False)
        self.widget.setAlternatingRowColors(False)
        self.widget.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.widget.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.widget.setSortingEnabled(False)
        set_header_fit_column_widths(
            self.widget,
            {
                0: 46,
                1: 62,
                self.TARGET_MHZ_COLUMN: 108,
                self.MEASURED_MHZ_COLUMN: 128,
                self.FPS_COLUMN: 70,
                self.FPS_DELTA_COLUMN: 112,
                self.POWER_COLUMN: 92,
                self.POWER_DELTA_COLUMN: 126,
                self.TEMP_COLUMN: 82,
                self.FAN_COLUMN: 74,
                self.FPSW_COLUMN: 96,
                self.FPSW_DELTA_COLUMN: 126,
                self.BUDGET_COLUMN: 132,
                self.DECISION_COLUMN: 106,
                self.STATUS_COLUMN: 190,
            },
            QtCore=QtCore,
            padding=34,
        )

    def clear(self) -> None:
        self.base_baseline = None
        self._progress_by_probe.clear()
        self.widget.setRowCount(0)

    def add_probe_start(self, payload: dict) -> None:
        row = self.widget.rowCount()
        self.widget.insertRow(row)
        self._write_row(row, payload, running=True)

    def update_probe_progress(self, payload: dict) -> None:
        row = self._find_running_row(payload)
        if row is None:
            return
        progress = {
            "elapsed_s": _to_float(payload.get("elapsed_s")),
            "target_duration_s": _to_float(payload.get("target_duration_s")),
            "label": _progress_label(payload),
        }
        self._progress_by_probe[_probe_key(payload)] = progress
        self._set_status_progress_cell(
            row,
            progress=progress,
            running=True,
            row_state="running",
        )

    def mark_running_rows_stopping(self) -> None:
        for row in self._active_rows():
            self._set_decision_cell(row, "stopping", row_state="warning")
            self._apply_row_state(row, "warning")
            self._set_status_progress_cell(
                row,
                progress=self._row_progress(row, label="Stopping"),
                running=True,
                row_state="warning",
            )

    def mark_running_rows_stopped(self, *, label: str = "Stopped") -> None:
        for row in self._active_rows():
            self._set_decision_cell(row, "stopped", row_state="error")
            self._apply_row_state(row, "error")
            self._set_status_progress_cell(
                row,
                progress=self._row_progress(row, label=label),
                running=False,
                row_state="error",
            )

    def add_probe_result(self, payload: dict) -> None:
        if _is_base_baseline(payload):
            self.base_baseline = dict(payload)
        row = self._find_running_row(payload)
        if row is None:
            row = self.widget.rowCount()
            self.widget.insertRow(row)
        self._write_row(row, payload, running=False)

    def _write_row(self, row: int, payload: dict, *, running: bool) -> None:
        row_state = _row_state(payload, running=running)
        values = self._row_values(row, payload, running=running)
        for column, value in enumerate(values):
            item = self.widget_item(str(value), row_state=row_state, column=column)
            self.widget.setItem(row, column, item)
        self._paint_delta_cell(
            row,
            self.FPS_DELTA_COLUMN,
            _to_float(payload.get("fps")),
            higher_is_better=True,
            bold=False,
        )
        self._paint_delta_cell(
            row,
            self.POWER_DELTA_COLUMN,
            _to_float(payload.get("power_w")),
            higher_is_better=False,
            bold=True,
        )
        self._paint_fpsw_cell(row, payload)
        self._paint_fpsw_delta_cell(row, payload)
        self._set_budget_cell(row, payload)
        key = _probe_key(payload)
        progress = self._progress_by_probe.get(key)
        if progress is None:
            progress = {
                "elapsed_s": _to_float(payload.get("elapsed_s")),
                "target_duration_s": _to_float(payload.get("target_duration_s")),
                "label": _progress_label(payload),
            }
        self._set_status_progress_cell(
            row,
            progress=progress,
            running=running,
            row_state=row_state,
        )
        self._scroll_to_latest(row)

    def _row_values(self, row: int, payload: dict, *, running: bool) -> list[str]:
        decision = "running" if running else payload.get("decision", payload.get("status", ""))
        return [
            f"{int(row) + 1}.",
            _format_int(payload.get("voltage_mv")),
            _format_int(payload.get("clock_mhz")),
            _format_float(_measured_clock_value(payload)),
            _format_float(payload.get("fps")),
            self._delta_text(payload.get("fps"), "fps"),
            _format_float(payload.get("power_w")),
            self._delta_text(payload.get("power_w"), "power_w"),
            _format_float(payload.get("temp_c")),
            _format_float(payload.get("fan_pct")),
            _format_float(payload.get("efficiency_fps_per_w")),
            self._delta_text(
                payload.get("efficiency_fps_per_w"),
                "efficiency_fps_per_w",
            ),
            "",
            str(decision),
            "",
        ]

    def _delta_text(
        self,
        value,
        baseline_key: str,
    ) -> str:
        if self.base_baseline is None:
            return ""
        if _is_base_baseline(self.base_baseline):
            baseline = _to_float(self.base_baseline.get(baseline_key))
        else:
            baseline = None
        current = _to_float(value)
        if current is None or baseline is None or baseline == 0.0:
            return ""
        if current == baseline:
            return "ref" if baseline_key == "fps" else "ref"
        raw_delta_pct = ((current - baseline) / baseline) * 100.0
        return f"{raw_delta_pct:+.2f}%"

    def _baseline_value(self, baseline_key: str) -> float | None:
        if self.base_baseline is None or not _is_base_baseline(self.base_baseline):
            return None
        return _to_float(self.base_baseline.get(baseline_key))

    def _paint_delta_cell(
        self,
        row: int,
        column: int,
        value: float | None,
        *,
        higher_is_better: bool,
        bold: bool,
    ) -> None:
        item = self.widget.item(row, column)
        if item is None or self.base_baseline is None or value is None:
            return
        baseline_key = "fps" if higher_is_better else "power_w"
        baseline = _to_float(self.base_baseline.get(baseline_key))
        if baseline is None or baseline == 0.0:
            return
        delta_pct = ((float(value) - baseline) / baseline) * 100.0
        if abs(delta_pct) < 0.05:
            return

        if higher_is_better:
            if delta_pct > 0.05:
                color = "#55d27a"
            elif delta_pct >= -1.5:
                color = "#f0b84c"
            else:
                color = "#ff6b6b"
        elif delta_pct <= -3.0:
            color = "#55d27a"
        elif delta_pct <= 2.0:
            color = "#f0b84c"
        else:
            color = "#ff6b6b"
        item.setForeground(self.QtGui.QColor(color))
        if bold:
            item.setFont(_bold_font(item))

    def _paint_fpsw_cell(self, row: int, payload: dict) -> None:
        item = self.widget.item(row, self.FPSW_COLUMN)
        value = _to_float(payload.get("efficiency_fps_per_w"))
        if item is None or value is None or self.base_baseline is None:
            return
        baseline = _to_float(self.base_baseline.get("efficiency_fps_per_w"))
        if baseline is None or baseline == 0.0:
            return
        if _is_base_baseline(payload):
            item.setToolTip("Base FPS/W reference")
            return
        delta_pct = ((float(value) - baseline) / baseline) * 100.0
        if delta_pct > 0.5:
            color = "#55d27a"
        elif delta_pct >= -0.5:
            color = "#f0b84c"
        else:
            color = "#ff6b6b"
        item.setForeground(self.QtGui.QColor(color))
        item.setFont(_bold_font(item))
        item.setToolTip(f"FPS/W {delta_pct:+.2f}% vs base")

    def _paint_fpsw_delta_cell(self, row: int, payload: dict) -> None:
        item = self.widget.item(row, self.FPSW_DELTA_COLUMN)
        value = _to_float(payload.get("efficiency_fps_per_w"))
        if item is None or value is None or self.base_baseline is None:
            return
        baseline = _to_float(self.base_baseline.get("efficiency_fps_per_w"))
        if baseline is None or baseline == 0.0:
            return
        if _is_base_baseline(payload):
            item.setToolTip("Base FPS/W reference")
            return
        delta_pct = ((float(value) - baseline) / baseline) * 100.0
        if delta_pct > 0.5:
            color = "#55d27a"
        elif delta_pct >= -0.5:
            color = "#f0b84c"
        else:
            color = "#ff6b6b"
        item.setForeground(self.QtGui.QColor(color))
        item.setFont(_bold_font(item))
        item.setToolTip(f"FPS/W {delta_pct:+.2f}% vs base")

    def _set_budget_cell(self, row: int, payload: dict) -> None:
        used = _to_float(payload.get("overclock_budget_used_pct"))
        limit = _to_float(payload.get("overclock_budget_limit_pct"))
        if used is None or limit is None:
            self.widget.removeCellWidget(row, self.BUDGET_COLUMN)
            return

        bar = self.QtWidgets.QProgressBar()
        bar.setRange(0, 1000)
        bar.setTextVisible(False)
        bar.setFixedHeight(18)
        if float(limit) <= 0.0:
            ratio = 0.0
            bar.setFormat("")
        else:
            ratio = max(0.0, min(1.0, float(used) / float(limit)))
            bar.setFormat("")
        fill = _budget_fill_color()
        bar.setValue(int(round(ratio * 1000.0)))
        display_used, display_limit = _budget_display_values(used, limit)
        bar.setToolTip(
            "Overclocking budget used: "
            f"{display_used:.2f} of {display_limit:.2f} internal clock-percent points"
        )
        bar.setStyleSheet(
            _progress_bar_stylesheet(fill, text_color=_progress_text_color(fill, ratio))
        )
        self.widget.setCellWidget(row, self.BUDGET_COLUMN, bar)

    def _set_status_progress_cell(
        self,
        row: int,
        *,
        progress: dict | None,
        running: bool,
        row_state: str,
    ) -> None:
        elapsed = _to_float((progress or {}).get("elapsed_s"))
        target = _to_float((progress or {}).get("target_duration_s"))
        label = str((progress or {}).get("label") or "").strip()
        bar = self.QtWidgets.QProgressBar()
        bar.setTextVisible(True)
        bar.setFixedHeight(18)

        fill = _row_colors(row_state)["accent"]
        if running and row_state == "running":
            fill = "#7fb4ff"
        ratio = None
        if target is not None and float(target) > 0.0:
            shown_elapsed = min(max(0.0, float(elapsed or 0.0)), float(target))
            ratio = min(1.0, shown_elapsed / float(target))
            if not running and row_state in {"good", "baseline", "neutral"}:
                ratio = 1.0
                shown_elapsed = float(target)
            bar.setRange(0, 1000)
            bar.setValue(int(round(ratio * 1000.0)))
            time_text = _progress_time_text(shown_elapsed, target)
            bar.setFormat(f"{label} {time_text}" if label else time_text)
            bar.setToolTip(
                f"Stability progress: {_format_duration_compact(shown_elapsed)} / "
                f"{_format_duration_compact(target)}"
            )
        elif running:
            existing = self.widget.cellWidget(row, self.STATUS_COLUMN)
            if isinstance(existing, self._busy_progress_class):
                busy = existing
                busy.set_progress_style(
                    label=label,
                    fill=fill,
                    text_color=_progress_text_color(fill, ratio),
                )
            else:
                busy = self._busy_progress_class(
                    label=label,
                    fill=fill,
                    text_color=_progress_text_color(fill, ratio),
                )
            busy.setToolTip("Stability progress is waiting for target duration data")
            self.widget.setCellWidget(row, self.STATUS_COLUMN, busy)
            return
        else:
            bar.setRange(0, 1000)
            ratio = 1.0
            bar.setValue(1000)
            bar.setFormat(f"{label} 100%" if label else "100%")
            bar.setToolTip("Stability probe finished")

        bar.setStyleSheet(
            _progress_bar_stylesheet(fill, text_color=_progress_text_color(fill, ratio))
        )
        self.widget.setCellWidget(row, self.STATUS_COLUMN, bar)

    def _find_running_row(self, payload: dict) -> int | None:
        voltage = _format_int(payload.get("voltage_mv"))
        clock = _format_int(payload.get("clock_mhz"))
        for row in range(self.widget.rowCount() - 1, -1, -1):
            if not _is_active_decision(self._cell_text(row, self.DECISION_COLUMN)):
                continue
            if (
                self._cell_text(row, 1) == voltage
                and self._cell_text(row, self.TARGET_MHZ_COLUMN) == clock
            ):
                return row
        return None

    def _cell_text(self, row: int, column: int) -> str:
        item = self.widget.item(row, column)
        return "" if item is None else item.text()

    def _active_rows(self) -> list[int]:
        return [
            row
            for row in range(self.widget.rowCount())
            if _is_active_decision(self._cell_text(row, self.DECISION_COLUMN))
        ]

    def _row_progress(self, row: int, *, label: str) -> dict:
        item = self.widget.item(row, 0)
        probe_key = None
        if item is not None:
            voltage = self._cell_text(row, 1)
            clock = self._cell_text(row, self.TARGET_MHZ_COLUMN)
            probe_key = (voltage, clock)
        progress = dict(self._progress_by_probe.get(probe_key, {}))
        progress["label"] = str(label)
        return progress

    def _set_decision_cell(self, row: int, text: str, *, row_state: str) -> None:
        item = self.widget_item(
            str(text),
            row_state=row_state,
            column=self.DECISION_COLUMN,
        )
        self.widget.setItem(row, self.DECISION_COLUMN, item)

    def _apply_row_state(self, row: int, row_state: str) -> None:
        colors = _row_colors(row_state)
        for column in range(self.widget.columnCount()):
            item = self.widget.item(row, column)
            if item is None:
                continue
            if column == self.DECISION_COLUMN:
                item.setBackground(self.QtGui.QColor("#171b21"))
                item.setForeground(self.QtGui.QColor("#d8dee9"))
                continue
            item.setBackground(self.QtGui.QColor(colors["background"]))
            item.setForeground(self.QtGui.QColor(colors["foreground"]))

    def selected_candidate_id(self) -> str | None:
        selected = self.widget.selectionModel().selectedRows()
        if not selected:
            return None
        row = int(selected[-1].row())
        voltage = self._cell_text(row, 1).strip()
        clock = self._cell_text(row, self.TARGET_MHZ_COLUMN).strip()
        if not voltage or not clock:
            return None
        return f"{voltage}mv-{clock}mhz"

    def _scroll_to_latest(self, row: int) -> None:
        item = self.widget.item(row, 0)
        if item is not None:
            self.widget.scrollToItem(
                item,
                self.QtWidgets.QAbstractItemView.PositionAtBottom,
            )

    def widget_item(self, text: str, *, row_state: str, column: int):
        item = self.QtWidgets.QTableWidgetItem(text)
        if column in {
            1,
            self.TARGET_MHZ_COLUMN,
            self.MEASURED_MHZ_COLUMN,
            self.FPS_COLUMN,
            self.FPS_DELTA_COLUMN,
            self.POWER_COLUMN,
            self.POWER_DELTA_COLUMN,
            8,
            9,
            self.FPSW_COLUMN,
            self.FPSW_DELTA_COLUMN,
        }:
            item.setTextAlignment(
                self.QtCore.Qt.AlignRight | self.QtCore.Qt.AlignVCenter
            )
        colors = _row_colors(row_state)
        item.setBackground(self.QtGui.QColor(colors["background"]))
        item.setForeground(self.QtGui.QColor(colors["foreground"]))
        if column == self.DECISION_COLUMN:
            item.setBackground(self.QtGui.QColor("#171b21"))
            item.setForeground(self.QtGui.QColor("#d8dee9"))
        return item


def _row_state(payload: dict, *, running: bool) -> str:
    if running:
        return "running"
    stage = str(payload.get("stage", "")).lower()
    decision = str(payload.get("decision", payload.get("status", ""))).lower()
    reason = str(payload.get("reason", "")).lower()
    text = f"{decision} {reason}"
    if "base" in stage or "stock" in stage or stage == "baseline":
        if "fail" not in text and "error" not in text:
            return "baseline"
    if any(token in text for token in ("fail", "failed", "error", "crash", "stall", "timeout", "recover-upward")):
        return "error"
    if any(
        token in text
        for token in (
            "warn",
            "guardrail",
            "floor",
            "miss",
            "retry",
            "overclock",
            "reject",
            "budget",
        )
    ):
        return "warning"
    if decision in {"accept", "pass"}:
        return "good"
    return "neutral"


def _row_colors(row_state: str) -> dict[str, str]:
    if row_state == "good":
        return {
            "background": "#14271c",
            "foreground": "#dfffe6",
            "accent": "#62e887",
        }
    if row_state == "warning":
        return {
            "background": "#302514",
            "foreground": "#ffe5b1",
            "accent": "#f0b84c",
        }
    if row_state == "error":
        return {
            "background": "#35191d",
            "foreground": "#ffd7d7",
            "accent": "#ff6b6b",
        }
    if row_state == "running":
        return {
            "background": "#172336",
            "foreground": "#d7e8ff",
            "accent": "#7fb4ff",
        }
    if row_state == "baseline":
        return {
            "background": "#1b2430",
            "foreground": "#e8eef7",
            "accent": "#9aa4b2",
        }
    return {
        "background": "#171b21",
        "foreground": "#d8dee9",
        "accent": "#d8dee9",
    }


def _bold_font(item):
    font = item.font()
    font.setBold(True)
    return font


def _progress_text_color(fill: str, ratio: float | None) -> str:
    if ratio is None or float(ratio) < 0.995:
        return "#f2f5f2"
    return "#fff4f4" if fill.lower() == "#ff6b6b" else "#10140f"


def _progress_bar_stylesheet(fill: str, *, text_color: str = "#f2f5f2") -> str:
    return (
        "QProgressBar {"
        "background: #111418;"
        "border: 1px solid #2e3440;"
        "border-radius: 3px;"
        f"color: {text_color};"
        "font-size: 11px;"
        "font-weight: 800;"
        "text-align: center;"
        "}"
        f"QProgressBar::chunk {{ background: {fill}; border-radius: 2px; }}"
    )


def _busy_progress_class(QtCore, QtGui, QtWidgets):
    class BusyBounceProgress(QtWidgets.QWidget):
        def __init__(
            self,
            *,
            label: str,
            fill: str,
            text_color: str = "#f2f5f2",
        ):
            super().__init__()
            self._label = str(label or "")
            self._fill = str(fill or "#7fb4ff")
            self._text_color = str(text_color or "#f2f5f2")
            self._frame = 0
            self.setFixedHeight(18)
            self.setMinimumWidth(80)
            self._timer = QtCore.QTimer(self)
            self._timer.setInterval(70)
            self._timer.timeout.connect(self._advance)
            self._timer.start()

        def set_progress_style(
            self,
            *,
            label: str,
            fill: str,
            text_color: str = "#f2f5f2",
        ) -> None:
            self._label = str(label or "")
            self._fill = str(fill or "#7fb4ff")
            self._text_color = str(text_color or "#f2f5f2")
            self.update()

        def _advance(self) -> None:
            self._frame += 1
            self.update()

        def paintEvent(self, _event) -> None:
            painter = QtGui.QPainter(self)
            painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
            outer = self.rect().adjusted(0, 0, -1, -1)
            inner = outer.adjusted(2, 2, -2, -2)
            painter.setPen(QtGui.QColor("#2e3440"))
            painter.setBrush(QtGui.QColor("#111418"))
            painter.drawRoundedRect(outer, 3, 3)

            available_width = max(1, int(inner.width()))
            chunk_width = max(22, min(available_width, int(available_width * 0.34)))
            travel = max(0, available_width - chunk_width)
            position = _bounce_position_for_frame(self._frame)
            chunk_left = int(inner.left() + round(travel * position))
            chunk_rect = QtCore.QRect(
                chunk_left,
                inner.top(),
                chunk_width,
                inner.height(),
            )
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(QtGui.QColor(self._fill))
            painter.drawRoundedRect(chunk_rect, 2, 2)

            if self._label:
                painter.setPen(QtGui.QColor(self._text_color))
                font = painter.font()
                font.setPointSize(8)
                font.setBold(True)
                painter.setFont(font)
                painter.drawText(outer, QtCore.Qt.AlignCenter, self._label)

    return BusyBounceProgress


def _bounce_position_for_frame(frame: int, *, steps: int = 18) -> float:
    steps = max(1, int(steps))
    cycle = steps * 2 + 2
    index = int(frame) % cycle
    if index <= steps:
        return float(index) / float(steps)
    if index == steps + 1:
        return 1.0
    return float(cycle - 1 - index) / float(steps)


def _is_base_baseline(payload: dict) -> bool:
    stage = str(payload.get("stage", "")).lower()
    return stage in {"base-baseline", "stock-baseline"}


def _probe_key(payload: dict) -> tuple[str, str]:
    return (_format_int(payload.get("voltage_mv")), _format_int(payload.get("clock_mhz")))


def _is_active_decision(text: str) -> bool:
    return str(text or "").strip().lower() in {"running", "stopping"}


def _progress_label(payload: dict) -> str:
    stage = str(payload.get("stage", "")).lower()
    if "final" in stage:
        return "Final verification"
    return ""


def _progress_time_text(elapsed_s, target_s) -> str:
    elapsed = _clamped_elapsed_s(elapsed_s, target_s)
    return f"{_format_duration_compact(elapsed)} / {_format_duration_compact(target_s)}"


def _clamped_elapsed_s(elapsed_s, target_s) -> float:
    try:
        elapsed = max(0.0, float(elapsed_s))
        target = max(0.0, float(target_s))
    except (TypeError, ValueError):
        return 0.0
    if target > 0.0:
        return min(elapsed, target)
    return elapsed


def _format_duration_compact(seconds) -> str:
    if seconds in (None, ""):
        return "n/a"
    try:
        total_seconds = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        return "n/a"
    if total_seconds < 60:
        return f"{total_seconds}s"
    if total_seconds < 3600:
        minutes, remainder_seconds = divmod(total_seconds, 60)
        if remainder_seconds:
            return f"{minutes}min {remainder_seconds}s"
        return f"{minutes}min"
    hours, remainder_seconds = divmod(total_seconds, 3600)
    minutes = int(round(remainder_seconds / 60.0))
    if minutes >= 60:
        hours += 1
        minutes = 0
    if minutes:
        return f"{hours}h {minutes}min"
    return f"{hours}h"


def _budget_display_values(used, limit) -> tuple[float, float]:
    used_value = max(0.0, float(used))
    limit_value = max(0.0, float(limit))
    if limit_value > 0.0:
        used_value = min(used_value, limit_value)
    return used_value, limit_value


def _budget_fill_color() -> str:
    return "#55d27a"


def _measured_clock_value(payload: dict):
    return payload.get("measured_clock_mhz", payload.get("avg_core_clock_mhz"))


def _to_float(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_int(value) -> str:
    number = _to_float(value)
    if number is None:
        return ""
    return str(int(round(number)))


def _format_float(value) -> str:
    number = _to_float(value)
    if number is None:
        return ""
    return f"{number:.2f}"
