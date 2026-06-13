"""Coverage for ui/components/vf_curve_editor.py.

The editor is one big dialog builder full of nested handler closures. We drive
it by monkeypatching QDialog.exec so construction runs to completion, then we
fire the wired shortcuts and buttons (captured via the live dialog) to exercise
the interactive handlers. Pure helpers are tested directly.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from ui.components.vf_curve_editor import (
    _nearest_curve_point,
    open_vf_curve_editor_dialog,
    vf_curve_editor_shortcut_legend_rows,
)
from ui.qt import import_qt


def _plan() -> list[dict]:
    return [
        {
            "index": i,
            "voltage_mv": 800 + i * 25,
            "base_mhz": 2000 + i * 20,
            "target_mhz": 2000 + i * 20,
        }
        for i in range(6)
    ]


def _base_points() -> list[tuple[float, float]]:
    return [(800 + i * 25, 2000 + i * 20) for i in range(6)]


# --- pure helpers -------------------------------------------------------------


def test_shortcut_legend_rows() -> None:
    rows = vf_curve_editor_shortcut_legend_rows()
    assert ("Drag", "move selected bin") in rows
    assert ("Ctrl+Z / Ctrl+Y", "undo / redo") in rows


def test_nearest_curve_point_picks_closest_within_threshold() -> None:
    points = [(800.0, 2000.0), (900.0, 2400.0)]
    view_range = ((800.0, 1000.0), (2000.0, 2800.0))
    assert _nearest_curve_point(805, 2010, points, view_range) == (800.0, 2000.0)
    # Far from any point -> rejected by the normalized distance threshold.
    assert _nearest_curve_point(950, 2000, points, view_range) is None
    assert _nearest_curve_point(800, 2000, [], view_range) is None


def test_nearest_curve_point_handles_bad_view_range() -> None:
    points = [(800.0, 2000.0)]
    # Malformed view range -> spans fall back to 1.0 without raising.
    assert _nearest_curve_point(800, 2000, points, "bad-range") == (800.0, 2000.0)


# --- dialog -------------------------------------------------------------------


def test_pg_none_shows_message_and_returns_false(qapp, monkeypatch) -> None:
    qtcore, qtgui, qtwidgets, _pg = import_qt()
    shown = []
    monkeypatch.setattr(
        qtwidgets.QMessageBox, "information", lambda *a, **k: shown.append(a)
    )
    result = open_vf_curve_editor_dialog(
        QtCore=qtcore,
        QtGui=qtgui,
        QtWidgets=qtwidgets,
        pg=None,
        parent=None,
        plan=_plan(),
        base_points=_base_points(),
        anchor=(900, 2400),
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
        # Fire each wired shortcut (undo/redo/nudge/select/lock/range handlers).
        for shortcut in getattr(dialog, "_curve_editor_shortcuts", []):
            try:
                shortcut.activated.emit()
            except Exception:
                pass
        # Click each push button (revert/undo/redo/save/cancel handlers).
        for button in dialog.findChildren(qtwidgets.QPushButton):
            try:
                button.click()
            except Exception:
                pass
        return qtwidgets.QDialog.Rejected

    monkeypatch.setattr(qtwidgets.QDialog, "exec", _exec)

    result = open_vf_curve_editor_dialog(
        QtCore=qtcore,
        QtGui=qtgui,
        QtWidgets=qtwidgets,
        pg=pg,
        parent=None,
        plan=_plan(),
        base_points=_base_points(),
        anchor=(850, 2050),
        save_callback=lambda edit: saved.append(edit) or "saved",
        control_voltage_mvs=(800, 825),
    )
    # exec was monkeypatched to Rejected, so the call reports not-accepted.
    assert result is False
    # Clicking Save ran the save callback at least once.
    assert saved
