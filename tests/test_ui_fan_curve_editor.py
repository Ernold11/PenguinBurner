"""Coverage for ui/components/fan_curve_editor.py.

Same strategy as the VF editor: monkeypatch QDialog.exec so construction runs,
then fire the wired shortcuts/buttons via the live dialog to drive the handler
closures. Pure helpers tested directly.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from ui.components.fan_curve_editor import (
    _nearest_curve_point,
    fan_curve_editor_shortcut_legend_rows,
    open_fan_curve_editor_dialog,
)
from ui.qt import import_qt


def _curve_points() -> list[tuple[float, float]]:
    return [(30.0, 20.0), (50.0, 40.0), (70.0, 60.0), (85.0, 85.0)]


def test_shortcut_legend_rows_present() -> None:
    rows = fan_curve_editor_shortcut_legend_rows()
    assert rows and all(len(row) == 2 for row in rows)


def test_nearest_curve_point_threshold() -> None:
    points = [(30.0, 20.0), (70.0, 60.0)]
    view_range = ((20.0, 90.0), (0.0, 100.0))
    assert _nearest_curve_point(31, 21, points, view_range) == (30.0, 20.0)
    assert _nearest_curve_point(20, 99, points, view_range) is None
    assert _nearest_curve_point(30, 20, [], view_range) is None


def test_pg_none_returns_false(qapp, monkeypatch) -> None:
    qtcore, qtgui, qtwidgets, _pg = import_qt()
    shown = []
    monkeypatch.setattr(
        qtwidgets.QMessageBox, "information", lambda *a, **k: shown.append(a)
    )
    result = open_fan_curve_editor_dialog(
        QtCore=qtcore,
        QtGui=qtgui,
        QtWidgets=qtwidgets,
        pg=None,
        parent=None,
        curve_points=_curve_points(),
        measured_points=[],
        target_point=None,
        save_callback=lambda _edit: None,
    )
    assert result is False
    assert shown


def test_dialog_builds_and_handlers_run(qapp, monkeypatch) -> None:
    qtcore, qtgui, qtwidgets, pg = import_qt()
    if pg is None:
        pytest.skip("pyqtgraph not available")

    saved: list = []

    def _exec(dialog):
        for shortcut in getattr(dialog, "_fan_curve_editor_shortcuts", []):
            try:
                shortcut.activated.emit()
            except Exception:
                pass
        for button in dialog.findChildren(qtwidgets.QPushButton):
            try:
                button.click()
            except Exception:
                pass
        return qtwidgets.QDialog.Rejected

    monkeypatch.setattr(qtwidgets.QDialog, "exec", _exec)

    result = open_fan_curve_editor_dialog(
        QtCore=qtcore,
        QtGui=qtgui,
        QtWidgets=qtwidgets,
        pg=pg,
        parent=None,
        curve_points=_curve_points(),
        measured_points=[(40.0, 30.0), (60.0, 55.0)],
        target_point=(50.0, 40.0),
        save_callback=lambda edit: saved.append(edit) or "saved",
    )
    assert result is False
    assert saved
