from __future__ import annotations


class CurvePlot:
    def __init__(
        self,
        *,
        QtWidgets,
        pg,
        x_label: str,
        x_units: str = "",
        y_label: str,
        y_units: str,
        x_range: tuple[float, float] | None = None,
        y_range: tuple[float, float] | None = None,
        source_name: str = "Base",
        candidate_name: str = "Candidate",
        show_source: bool = True,
        source_color: str = "#c8cdd5",
        candidate_color: str = "#5ef38c",
    ):
        self.pg = pg
        self._x_label = str(x_label)
        self._x_units = str(x_units)
        self._y_label = str(y_label)
        self._y_units = str(y_units)
        self._source_color = str(source_color)
        self._candidate_color = str(candidate_color)
        self.source_curve = None
        self.candidate_curve = None
        self.probe_marker = None
        self.probe_vline = None
        self.probe_hline = None
        self.probe_x_axis_badge = None
        self.probe_y_axis_badge = None
        self.previous_curves = []
        self._source_points: list[tuple[float, float]] = []
        self._candidate_points: list[tuple[float, float]] = []
        self._last_candidate_points: list[tuple[float, float]] = []
        self._last_live_probe_values: tuple[float, float] | None = None
        self._point_selection_enabled = False
        if pg is None:
            widget = QtWidgets.QPlainTextEdit()
            widget.setReadOnly(True)
            widget.setPlainText("pyqtgraph is not installed.")
            self.widget = widget
            return

        plot = pg.PlotWidget()
        if hasattr(pg, "setConfigOptions"):
            pg.setConfigOptions(antialias=True)
        plot.setBackground("#111418")
        plot.showGrid(x=True, y=True, alpha=0.22)
        plot.setLabel("bottom", x_label, units=x_units)
        plot.setLabel("left", y_label, units=y_units)
        _disable_axis_si_prefix(plot, "bottom")
        _disable_axis_si_prefix(plot, "left")
        if x_range is not None:
            plot.setXRange(float(x_range[0]), float(x_range[1]), padding=0.0)
        if y_range is not None:
            plot.setYRange(float(y_range[0]), float(y_range[1]), padding=0.0)
        plot.addLegend(offset=(-24, -54))
        if show_source:
            self.source_curve = plot.plot(
                [],
                [],
                pen=pg.mkPen(self._source_color, width=1),
                symbol="o",
                symbolBrush=self._source_color,
                symbolSize=5,
                name=str(source_name),
            )
        self.candidate_curve = plot.plot(
            [],
            [],
            pen=pg.mkPen(self._candidate_color, width=2),
            symbol="o",
            symbolBrush=self._candidate_color,
            symbolSize=6,
            name=str(candidate_name),
        )
        self.probe_marker = plot.plot(
            [],
            [],
            pen=None,
            symbol="o",
            symbolPen=pg.mkPen(pg.mkColor(255, 229, 177, 235), width=1),
            symbolBrush=pg.mkBrush(pg.mkColor(240, 184, 76, 230)),
            symbolSize=10,
        )
        probe_line_pen = pg.mkPen(pg.mkColor(240, 184, 76, 135), width=1)
        self.probe_vline = pg.InfiniteLine(
            angle=90,
            movable=False,
            pen=probe_line_pen,
        )
        self.probe_hline = pg.InfiniteLine(
            angle=0,
            movable=False,
            pen=probe_line_pen,
        )
        plot.addItem(self.probe_vline, ignoreBounds=True)
        plot.addItem(self.probe_hline, ignoreBounds=True)
        self.probe_vline.hide()
        self.probe_hline.hide()
        badge_border = pg.mkPen(pg.mkColor(240, 184, 76, 120), width=1)
        badge_fill = pg.mkColor(17, 20, 24, 175)
        self.probe_x_axis_badge = pg.TextItem(
            "",
            color="#ffe5b1",
            anchor=(0.5, 1.0),
            border=badge_border,
            fill=badge_fill,
        )
        self.probe_y_axis_badge = pg.TextItem(
            "",
            color="#ffe5b1",
            anchor=(0.0, 0.5),
            border=badge_border,
            fill=badge_fill,
        )
        plot.addItem(self.probe_x_axis_badge, ignoreBounds=True)
        plot.addItem(self.probe_y_axis_badge, ignoreBounds=True)
        self.probe_x_axis_badge.hide()
        self.probe_y_axis_badge.hide()
        plot.getViewBox().sigRangeChanged.connect(
            lambda *_args: self._update_axis_probe_badges()
        )
        plot.scene().sigMouseClicked.connect(self._handle_curve_click)
        self.plot = plot
        self.widget = plot

    def enable_point_selection(self, enabled: bool = True) -> None:
        self._point_selection_enabled = bool(enabled)

    def set_source_points(self, points: list[tuple[float, float]]) -> None:
        self._source_points = _normalize_points(points)
        if self.source_curve is None:
            return
        self.source_curve.setData(
            [point[0] for point in self._source_points],
            [point[1] for point in self._source_points],
        )

    def set_base_points(self, points: list[tuple[float, float]]) -> None:
        self.set_source_points(points)

    def clear(self) -> None:
        self._source_points = []
        self._candidate_points = []
        self._last_candidate_points = []
        if self.source_curve is not None:
            self.source_curve.setData([], [])
        if self.candidate_curve is not None:
            self.candidate_curve.setData([], [])
        if self.pg is None or not hasattr(self, "plot"):
            return
        for trace in self.previous_curves:
            self.plot.removeItem(trace)
        self.previous_curves = []
        self.clear_load_markers()

    def set_candidate_points(
        self,
        points: list[tuple[float, float]],
        *,
        remember_previous: bool = True,
    ) -> None:
        if self.candidate_curve is None:
            return
        normalized = _normalize_points(points)
        self._candidate_points = normalized
        if (
            remember_previous
            and self._last_candidate_points
            and normalized != self._last_candidate_points
        ):
            self.add_previous_points(self._last_candidate_points)
        self._last_candidate_points = normalized
        self.candidate_curve.setData(
            [point[0] for point in normalized],
            [point[1] for point in normalized],
        )

    def add_previous_points(self, points: list[tuple[float, float]]) -> None:
        if self.pg is None or not hasattr(self, "plot"):
            return
        if not points:
            return
        trace = self.plot.plot(
            [point[0] for point in points],
            [point[1] for point in points],
            pen=self.pg.mkPen(self.pg.mkColor(94, 243, 140, 72), width=1),
            symbol=None,
        )
        self.previous_curves.append(trace)
        while len(self.previous_curves) > 12:
            old_trace = self.previous_curves.pop(0)
            self.plot.removeItem(old_trace)

    def clear_load_markers(self) -> None:
        if self.probe_marker is not None:
            self.probe_marker.setData([], [])
        if self.probe_vline is not None:
            self.probe_vline.hide()
        if self.probe_hline is not None:
            self.probe_hline.hide()
        self._last_live_probe_values = None
        self._hide_axis_probe_badges()
        self._restore_axis_labels()

    def set_target_point(self, voltage, clock) -> None:
        _set_probe_marker(self.probe_marker, voltage, clock)

    def set_crosshair_point(self, voltage, clock) -> None:
        if _set_probe_lines(self.probe_vline, self.probe_hline, voltage, clock):
            self._set_axis_probe_badges(voltage, clock)
        else:
            self._last_live_probe_values = None
            self._hide_axis_probe_badges()

    def set_selected_point(self, voltage, clock) -> None:
        self.set_target_point(voltage, clock)
        self.set_crosshair_point(voltage, clock)

    def set_probe_marker(self, payload: dict) -> None:
        voltage, clock = _probe_marker_values(
            payload,
            prefer_measured=False,
        )
        self.set_target_point(voltage, clock)

    def set_live_load_marker(self, payload: dict) -> None:
        voltage, clock = _probe_marker_values(payload, prefer_measured=True)
        self.set_crosshair_point(voltage, clock)

    def set_load_markers(self, payload: dict) -> None:
        self.set_live_load_marker(payload)

    def _handle_curve_click(self, event) -> None:
        if not self._point_selection_enabled or not hasattr(self, "plot"):
            return
        scene_pos = event.scenePos()
        if not self.plot.sceneBoundingRect().contains(scene_pos):
            return
        view_pos = self.plot.plotItem.vb.mapSceneToView(scene_pos)
        point = _nearest_curve_point(
            float(view_pos.x()),
            float(view_pos.y()),
            [*self._candidate_points, *self._source_points],
            self.plot.viewRange(),
        )
        if point is None:
            return
        self.set_selected_point(point[0], point[1])
        if hasattr(event, "accept"):
            event.accept()

    def _restore_axis_labels(self) -> None:
        if not hasattr(self, "plot"):
            return
        self.plot.setLabel("bottom", self._x_label, units=self._x_units)
        self.plot.setLabel("left", self._y_label, units=self._y_units)

    def _set_axis_probe_badges(self, voltage, clock) -> None:
        if not hasattr(self, "plot"):
            return
        try:
            voltage_value = float(voltage)
            clock_value = float(clock)
        except (TypeError, ValueError):
            self._last_live_probe_values = None
            self._hide_axis_probe_badges()
            return
        self._last_live_probe_values = (voltage_value, clock_value)
        self._update_axis_probe_badges()

    def _update_axis_probe_badges(self) -> None:
        if not hasattr(self, "plot") or self._last_live_probe_values is None:
            return
        if self.probe_x_axis_badge is None or self.probe_y_axis_badge is None:
            return
        voltage_value, clock_value = self._last_live_probe_values
        (x_min, x_max), (y_min, y_max) = self.plot.viewRange()
        x_span = max(1.0, float(x_max) - float(x_min))
        y_span = max(1.0, float(y_max) - float(y_min))
        x_badge_y = float(y_min) + y_span * 0.035
        y_badge_x = float(x_min) + x_span * 0.015
        self.probe_x_axis_badge.setText(
            _axis_value_badge_text(voltage_value, self._x_units)
        )
        self.probe_x_axis_badge.setPos(float(voltage_value), x_badge_y)
        self.probe_x_axis_badge.show()
        self.probe_y_axis_badge.setText(
            _axis_value_badge_text(clock_value, self._y_units)
        )
        self.probe_y_axis_badge.setPos(y_badge_x, float(clock_value))
        self.probe_y_axis_badge.show()

    def _hide_axis_probe_badges(self) -> None:
        if self.probe_x_axis_badge is not None:
            self.probe_x_axis_badge.hide()
        if self.probe_y_axis_badge is not None:
            self.probe_y_axis_badge.hide()


def _disable_axis_si_prefix(plot, axis_name: str) -> None:
    axis = plot.getAxis(axis_name)
    if hasattr(axis, "enableAutoSIPrefix"):
        axis.enableAutoSIPrefix(False)


def _set_probe_marker(marker, voltage, clock) -> None:
    if marker is None:
        return
    try:
        voltage_value = float(voltage)
        clock_value = float(clock)
    except (TypeError, ValueError):
        marker.setData([], [])
        return
    marker.setData([voltage_value], [clock_value])


def _set_probe_lines(vline, hline, voltage, clock) -> bool:
    try:
        voltage_value = float(voltage)
        clock_value = float(clock)
    except (TypeError, ValueError):
        if vline is not None:
            vline.hide()
        if hline is not None:
            hline.hide()
        return False
    if vline is not None:
        vline.setPos(voltage_value)
        vline.show()
    if hline is not None:
        hline.setPos(clock_value)
        hline.show()
    return True


def _normalize_points(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    normalized = []
    for point in points:
        try:
            normalized.append((float(point[0]), float(point[1])))
        except (IndexError, TypeError, ValueError):
            continue
    return normalized


def _nearest_curve_point(
    x_value: float,
    y_value: float,
    points: list[tuple[float, float]],
    view_range,
    *,
    max_normalized_distance: float = 0.04,
) -> tuple[float, float] | None:
    normalized = _normalize_points(points)
    if not normalized:
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
    for point in normalized:
        distance = (
            ((float(point[0]) - float(x_value)) / x_span) ** 2
            + ((float(point[1]) - float(y_value)) / y_span) ** 2
        ) ** 0.5
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_point = point
    if best_distance is None or best_distance > float(max_normalized_distance):
        return None
    return best_point


def _axis_value_badge_text(value: float, units: str) -> str:
    unit_text = f" {units}" if str(units).strip() else ""
    return f"{value:.0f}{unit_text}"


def _probe_marker_values(payload: dict, *, prefer_measured: bool) -> tuple[object, object]:
    if prefer_measured:
        voltage = _first_present(
            payload,
            "measured_voltage_mv",
            "avg_voltage_mv",
        )
        clock = _first_present(
            payload,
            "measured_clock_mhz",
            "avg_core_clock_mhz",
        )
    else:
        voltage = _first_present(
            payload,
            "voltage_mv",
            "measured_voltage_mv",
            "avg_voltage_mv",
        )
        clock = _first_present(
            payload,
            "clock_mhz",
            "measured_clock_mhz",
            "avg_core_clock_mhz",
        )
    return voltage, clock


def _first_present(payload: dict, *keys: str):
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None
