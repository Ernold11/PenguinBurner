from __future__ import annotations

from collections.abc import Callable

from manual_fan_curve_editor import (
    ManualFanCurveEdit,
    manual_add_fan_point_edit,
    manual_drag_fan_point_edit,
    manual_fan_curve_initial_edit,
    manual_nudge_selected_fan_speed,
    manual_nudge_selected_fan_temperature,
    manual_select_adjacent_fan_point,
    manual_select_fan_point,
)

from .. import theme
from .curve_editor import CurveEditHistory, install_curve_editor_shortcut_legend
from .curve_editor import nearest_curve_point
from .curve_plot import CurvePlot


def fan_curve_editor_shortcut_legend_rows() -> tuple[tuple[str, str], ...]:
    return (
        ("Click", "select dot"),
        ("Ctrl+Click", "new point"),
        ("Drag", "move dot"),
        ("Up/Down", "fan speed"),
        ("Left/Right", "temperature"),
        ("Tab / Shift+Tab", "next / previous"),
        ("Ctrl+Z / Ctrl+Y", "undo / redo"),
    )


def open_fan_curve_editor_dialog(
    *,
    QtCore,
    QtGui,
    QtWidgets,
    pg,
    parent,
    curve_points: list[tuple[float, float]],
    measured_points: list[tuple[float, float]],
    target_point: tuple[float, float] | None,
    save_callback: Callable[[ManualFanCurveEdit], str | None],
) -> bool:
    if pg is None:
        QtWidgets.QMessageBox.information(
            parent,
            "Edit Fan Curve",
            "pyqtgraph is not installed, so the fan curve editor is unavailable.",
        )
        return False

    def initial_fan_edit() -> ManualFanCurveEdit:
        return manual_fan_curve_initial_edit(curve_points, selected_point=target_point)

    current_edit = {"value": initial_fan_edit()}
    syncing_points = {"active": False}
    point_items: dict[int, object] = {}

    dialog = QtWidgets.QDialog(parent)
    dialog.setWindowTitle("Edit Fan Curve")
    dialog.resize(820, 560)
    layout = QtWidgets.QVBoxLayout(dialog)
    plot = CurvePlot(
        QtWidgets=QtWidgets,
        pg=pg,
        x_label="Temperature",
        x_units="C",
        y_label="Fan",
        y_units="%",
        x_range=(30, 95),
        y_range=(0, 100),
        source_name="Measured",
        candidate_name="Edited draft",
        show_source=bool(measured_points),
        source_color=theme.BASELINE,
        candidate_color=theme.PLOT_CANDIDATE,
    )
    if measured_points:
        plot.set_source_points(measured_points)
    plot.add_comparison_points(
        curve_points,
        name="Before edit",
        color=theme.PLOT_COMPARISON,
        alpha=145,
        width=1,
    )
    plot.set_candidate_points(curve_points, remember_previous=False)
    layout.addWidget(plot.widget, 1)
    legend_overlay, legend_filter = install_curve_editor_shortcut_legend(
        QtWidgets=QtWidgets,
        QtCore=QtCore,
        plot_widget=plot.widget,
        parent=dialog,
        rows=fan_curve_editor_shortcut_legend_rows(),
    )
    dialog._fan_curve_editor_shortcut_legend = legend_overlay
    dialog._fan_curve_editor_shortcut_legend_filter = legend_filter

    status_label = QtWidgets.QLabel()
    status_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
    layout.addWidget(status_label)

    button_row = QtWidgets.QHBoxLayout()
    revert_button = QtWidgets.QPushButton("Revert")
    button_row.addWidget(revert_button)
    undo_button = QtWidgets.QPushButton("Undo")
    undo_button.setToolTip("Undo last edit (Ctrl+Z)")
    button_row.addWidget(undo_button)
    redo_button = QtWidgets.QPushButton("Redo")
    redo_button.setToolTip("Redo last undone edit (Ctrl+Y)")
    button_row.addWidget(redo_button)
    button_row.addStretch(1)
    save_button = QtWidgets.QPushButton("Save")
    standard_pixmap = getattr(QtWidgets.QStyle, "StandardPixmap", QtWidgets.QStyle)
    save_icon_id = getattr(standard_pixmap, "SP_DialogSaveButton", None)
    if save_icon_id is not None:
        save_button.setIcon(dialog.style().standardIcon(save_icon_id))
    cancel_button = QtWidgets.QPushButton("Cancel")
    button_row.addWidget(save_button)
    button_row.addWidget(cancel_button)
    layout.addLayout(button_row)

    def clone_edit(edit: ManualFanCurveEdit) -> ManualFanCurveEdit:
        return ManualFanCurveEdit(
            points=[(float(x), float(y)) for x, y in edit.points],
            edit_kind=str(edit.edit_kind),
            selected_index=int(edit.selected_index),
        )

    def edit_signature(edit: ManualFanCurveEdit):
        return (
            str(edit.edit_kind),
            int(edit.selected_index),
            tuple((round(float(x), 3), round(float(y), 3)) for x, y in edit.points),
        )

    def edits_equal(left: ManualFanCurveEdit, right: ManualFanCurveEdit) -> bool:
        return edit_signature(left) == edit_signature(right)

    def selected_point(edit: ManualFanCurveEdit) -> tuple[float, float] | None:
        if not edit.points:
            return None
        index = max(0, min(len(edit.points) - 1, int(edit.selected_index)))
        return edit.points[index]

    def update_history_buttons() -> None:
        undo_button.setEnabled(bool(history.undo_stack))
        redo_button.setEnabled(bool(history.redo_stack))

    def set_preview(edit: ManualFanCurveEdit, *, record_undo: bool = False) -> None:
        if record_undo and not edits_equal(current_edit["value"], edit):
            history.push(current_edit["value"])
        current_edit["value"] = edit
        plot.set_candidate_points(edit.points, remember_previous=False)
        point = selected_point(edit)
        if point is not None:
            plot.set_selected_point(point[0], point[1])
        update_status(edit)
        refresh_point_handles(edit)

    history = CurveEditHistory(
        clone=clone_edit,
        equals=edits_equal,
        apply=lambda edit: set_preview(edit, record_undo=False),
        changed=update_history_buttons,
    )

    def update_status(edit: ManualFanCurveEdit) -> None:
        point = selected_point(edit)
        if point is None:
            status_label.setText("No editable fan curve point is selected.")
            return
        status_label.setText(
            "Selected fan point: "
            f"{float(point[0]):.0f} C / {float(point[1]):.0f}%. "
            "Drag or use arrow keys to tune the curve."
        )

    def point_item(entry):
        if isinstance(entry, dict):
            return entry.get("item")
        return entry

    def style_point_handle(item, *, selected: bool) -> None:
        if item is None:
            return
        if selected:
            item.setPen(pg.mkPen(pg.mkColor(255, 229, 177, 230), width=1))
            item.setBrush(pg.mkBrush(pg.mkColor(240, 184, 76, 225)))
        else:
            item.setPen(pg.mkPen(pg.mkColor(210, 226, 235, 190), width=1))
            item.setBrush(pg.mkBrush(pg.mkColor(94, 243, 140, 110)))

    def add_point_handle(index: int, temp_c: float, speed_pct: float) -> None:
        item = pg.TargetItem(
            pos=(float(temp_c), float(speed_pct)),
            size=9,
            symbol="o",
            pen=pg.mkPen(pg.mkColor(210, 226, 235, 190), width=1),
            brush=pg.mkBrush(pg.mkColor(94, 243, 140, 110)),
            movable=True,
        )
        if hasattr(item, "setZValue"):
            item.setZValue(6)
        item_mouse_click_event = item.mouseClickEvent

        def on_item_mouse_click_event(
            event,
            point_index=int(index),
            original_mouse_click=item_mouse_click_event,
        ) -> None:
            select_fan_point(point_index)
            original_mouse_click(event)

        item.mouseClickEvent = on_item_mouse_click_event

        def on_changed(*_args, point_index=int(index)) -> None:
            on_point_position_changed(point_index)

        def on_finished(*_args, point_index=int(index)) -> None:
            history.finish_action(
                f"fan-point:{int(point_index)}",
                current_edit["value"],
            )
            snap_point_handle(point_index)

        item.sigPositionChanged.connect(on_changed)
        item.sigPositionChangeFinished.connect(on_finished)
        plot.plot.addItem(item)
        point_items[int(index)] = {
            "item": item,
            "on_changed": on_changed,
            "on_finished": on_finished,
        }

    def refresh_point_handles(edit: ManualFanCurveEdit) -> None:
        for index in list(point_items):
            if index >= len(edit.points):
                item = point_item(point_items.pop(index))
                if item is not None:
                    plot.plot.removeItem(item)
        for index, point in enumerate(edit.points):
            if index not in point_items:
                add_point_handle(index, point[0], point[1])
        syncing_points["active"] = True
        try:
            for index, point in enumerate(edit.points):
                item = point_item(point_items.get(index))
                if item is None:
                    continue
                item.setPos(float(point[0]), float(point[1]))
                style_point_handle(item, selected=index == int(edit.selected_index))
        finally:
            syncing_points["active"] = False

    def snap_point_handle(index: int) -> None:
        if index < 0 or index >= len(current_edit["value"].points):
            return
        item = point_item(point_items.get(int(index)))
        if item is None:
            return
        temp_c, speed_pct = current_edit["value"].points[int(index)]
        syncing_points["active"] = True
        try:
            item.setPos(float(temp_c), float(speed_pct))
        finally:
            syncing_points["active"] = False

    def on_point_position_changed(index: int) -> None:
        if syncing_points["active"]:
            return
        item = point_item(point_items.get(int(index)))
        if item is None:
            return
        pos = item.pos()
        edit = manual_drag_fan_point_edit(
            current_edit["value"],
            point_index=int(index),
            requested_temp_c=float(pos.x()),
            requested_speed_pct=float(pos.y()),
        )
        if not edits_equal(edit, current_edit["value"]):
            history.begin_action(f"fan-point:{int(index)}", current_edit["value"])
        set_preview(edit)

    def select_fan_point(index: int) -> None:
        history.active_actions.clear()
        edit = manual_select_fan_point(current_edit["value"], point_index=int(index))
        set_preview(edit, record_undo=False)

    def select_adjacent_fan_point(direction: int) -> None:
        history.active_actions.clear()
        edit = manual_select_adjacent_fan_point(
            current_edit["value"],
            direction=int(direction),
        )
        set_preview(edit, record_undo=False)

    def nudge_selected_fan_speed(direction: int) -> None:
        history.active_actions.clear()
        edit = manual_nudge_selected_fan_speed(
            current_edit["value"],
            direction=int(direction),
        )
        set_preview(edit, record_undo=True)

    def nudge_selected_fan_temperature(direction: int) -> None:
        history.active_actions.clear()
        edit = manual_nudge_selected_fan_temperature(
            current_edit["value"],
            direction=int(direction),
        )
        set_preview(edit, record_undo=True)

    def nearest_fan_point_at_scene_pos(scene_pos):
        if not plot.plot.sceneBoundingRect().contains(scene_pos):
            return None
        view_pos = plot.plot.plotItem.vb.mapSceneToView(scene_pos)
        return nearest_curve_point(
            float(view_pos.x()),
            float(view_pos.y()),
            current_edit["value"].points,
            plot.plot.viewRange(),
            max_normalized_distance=0.08,
        )

    def create_fan_point_at_scene_pos(scene_pos) -> bool:
        if not plot.plot.sceneBoundingRect().contains(scene_pos):
            return False
        view_pos = plot.plot.plotItem.vb.mapSceneToView(scene_pos)
        history.active_actions.clear()
        edit = manual_add_fan_point_edit(
            current_edit["value"],
            requested_temp_c=float(view_pos.x()),
            requested_speed_pct=float(view_pos.y()),
        )
        set_preview(edit, record_undo=True)
        return True

    def event_has_keyboard_modifier(event, modifier_name: str) -> bool:
        modifier_enum = getattr(QtCore.Qt, "KeyboardModifier", QtCore.Qt)
        modifier = getattr(modifier_enum, str(modifier_name), None)
        modifiers_attr = getattr(event, "modifiers", None)
        if modifier is None or not callable(modifiers_attr):
            return False
        return bool(modifiers_attr() & modifier)

    def on_plot_clicked(event) -> None:
        if event_has_keyboard_modifier(event, "ControlModifier"):
            if not create_fan_point_at_scene_pos(event.scenePos()):
                return
            if hasattr(event, "accept"):
                event.accept()
            return
        point = nearest_fan_point_at_scene_pos(event.scenePos())
        if point is None:
            return
        edit = manual_select_fan_point(current_edit["value"], point=point)
        set_preview(edit, record_undo=False)
        if hasattr(event, "accept"):
            event.accept()

    def revert_edit() -> None:
        history.clear()
        set_preview(initial_fan_edit(), record_undo=False)
        update_history_buttons()

    key_press_event_type = getattr(
        getattr(QtCore.QEvent, "Type", QtCore.QEvent),
        "KeyPress",
        None,
    )
    key_enum = getattr(QtCore.Qt, "Key", QtCore.Qt)
    modifier_enum = getattr(QtCore.Qt, "KeyboardModifier", QtCore.Qt)
    key_left = getattr(key_enum, "Key_Left", None)
    key_right = getattr(key_enum, "Key_Right", None)
    key_up = getattr(key_enum, "Key_Up", None)
    key_down = getattr(key_enum, "Key_Down", None)
    key_tab = getattr(key_enum, "Key_Tab", None)
    key_backtab = getattr(key_enum, "Key_Backtab", None)
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
        focus_widget = QtWidgets.QApplication.focusWidget()
        if focus_widget is None:
            return bool(dialog.isActiveWindow())
        return bool(focus_widget is dialog or dialog.isAncestorOf(focus_widget))

    class FanCurveEditorKeyFilter(QtCore.QObject):
        def eventFilter(_filter_self, _watched, event) -> bool:
            if key_press_event_type is None or event.type() != key_press_event_type:
                return False
            if not dialog.isVisible() or not key_event_belongs_to_dialog():
                return False
            key = event.key()
            has_ctrl = event_has_modifier(event, control_modifier)
            has_shift = event_has_modifier(event, shift_modifier)
            has_alt = event_has_modifier(event, alt_modifier)
            if has_ctrl and not has_alt and key == key_z:
                history.undo(current_edit["value"])
            elif has_ctrl and not has_alt and key == key_y:
                history.redo(current_edit["value"])
            elif not has_ctrl and not has_shift and not has_alt and key == key_up:
                nudge_selected_fan_speed(1)
            elif not has_ctrl and not has_shift and not has_alt and key == key_down:
                nudge_selected_fan_speed(-1)
            elif not has_ctrl and not has_shift and not has_alt and key == key_left:
                nudge_selected_fan_temperature(-1)
            elif not has_ctrl and not has_shift and not has_alt and key == key_right:
                nudge_selected_fan_temperature(1)
            elif not has_ctrl and not has_alt and key == key_tab:
                select_adjacent_fan_point(-1 if has_shift else 1)
            elif not has_ctrl and not has_alt and key == key_backtab:
                select_adjacent_fan_point(-1)
            else:
                return False
            if hasattr(event, "accept"):
                event.accept()
            return True

    def save_edit() -> None:
        try:
            message = save_callback(current_edit["value"])
        except Exception as exc:
            QtWidgets.QMessageBox.critical(
                dialog,
                "Edit Fan Curve",
                f"Failed to save edited fan curve: {exc}",
            )
            return
        if message:
            status_label.setText(str(message))
        dialog.accept()

    plot.plot.scene().sigMouseClicked.connect(on_plot_clicked)
    refresh_point_handles(current_edit["value"])
    revert_button.clicked.connect(revert_edit)
    undo_button.clicked.connect(lambda: history.undo(current_edit["value"]))
    redo_button.clicked.connect(lambda: history.redo(current_edit["value"]))
    shortcut_context = getattr(
        getattr(QtCore.Qt, "ShortcutContext", QtCore.Qt),
        "WidgetWithChildrenShortcut",
        None,
    )
    shortcuts = []
    for sequence, callback in (
        ("Ctrl+Z", lambda: history.undo(current_edit["value"])),
        ("Ctrl+Y", lambda: history.redo(current_edit["value"])),
        ("Up", lambda: nudge_selected_fan_speed(1)),
        ("Down", lambda: nudge_selected_fan_speed(-1)),
        ("Left", lambda: nudge_selected_fan_temperature(-1)),
        ("Right", lambda: nudge_selected_fan_temperature(1)),
        ("Tab", lambda: select_adjacent_fan_point(1)),
        ("Shift+Tab", lambda: select_adjacent_fan_point(-1)),
    ):
        shortcut = QtGui.QShortcut(QtGui.QKeySequence(sequence), dialog)
        if shortcut_context is not None:
            shortcut.setContext(shortcut_context)
        shortcut.activated.connect(callback)
        shortcuts.append(shortcut)
    dialog._fan_curve_editor_shortcuts = shortcuts
    save_button.clicked.connect(save_edit)
    cancel_button.clicked.connect(dialog.reject)
    set_preview(current_edit["value"], record_undo=False)
    update_history_buttons()
    app_instance = QtWidgets.QApplication.instance()
    key_filter = FanCurveEditorKeyFilter(dialog)
    if app_instance is not None:
        app_instance.installEventFilter(key_filter)
    dialog._fan_curve_editor_key_filter = key_filter
    try:
        return bool(dialog.exec() == QtWidgets.QDialog.Accepted)
    finally:
        if app_instance is not None:
            app_instance.removeEventFilter(key_filter)


