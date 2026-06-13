"""Coverage for ui/components/curve_editor.py.

CurveEditHistory is a pure injected-callback state machine. The shortcut-legend
installer is exercised against a real (offscreen) plot widget incl. its toggle
button and resize event filter.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ui.components.curve_editor import (
    CurveEditHistory,
    install_curve_editor_shortcut_legend,
)
from ui.qt import import_qt


def _history(*, limit=64, with_changed=True):
    applied: list = []
    changed: list = []
    history = CurveEditHistory(
        clone=lambda value: value,
        equals=lambda a, b: a == b,
        apply=applied.append,
        changed=(lambda: changed.append(1)) if with_changed else None,
        limit=limit,
    )
    return history, applied, changed


def test_push_dedupes_consecutive_equal_snapshots() -> None:
    history, _applied, changed = _history()
    history.push("a")
    history.push("a")  # equal to top -> ignored
    history.push("b")
    assert history.undo_stack == ["a", "b"]
    assert sum(changed) == 2  # only the two real pushes notified


def test_undo_and_redo_roundtrip() -> None:
    history, applied, _changed = _history()
    history.push("a")
    history.push("b")
    history.undo("current")
    assert applied[-1] == "b"  # restored previous top
    assert history.redo_stack == ["current"]
    history.redo("now")
    assert applied[-1] == "current"
    assert history.undo_stack[-1] == "now"


def test_undo_redo_no_op_when_empty() -> None:
    history, applied, _changed = _history()
    history.undo("x")
    history.redo("x")
    assert applied == []


def test_begin_and_finish_action_drops_unchanged() -> None:
    history, _applied, _changed = _history()
    history.begin_action("drag", "v1")
    history.begin_action("drag", "v1")  # already active -> ignored
    assert history.undo_stack == ["v1"]
    # No change since begin -> the snapshot is popped on finish.
    history.finish_action("drag", "v1")
    assert history.undo_stack == []
    # Finishing an unknown action key is a no-op.
    history.finish_action("missing", "v2")


def test_finish_action_keeps_real_change() -> None:
    history, _applied, _changed = _history()
    history.begin_action("drag", "v1")
    history.finish_action("drag", "v2")  # changed -> snapshot retained
    assert history.undo_stack == ["v1"]


def test_clear_resets_all_stacks() -> None:
    history, _applied, _changed = _history()
    history.push("a")
    history.begin_action("k", "b")
    history.clear()
    assert history.undo_stack == []
    assert history.redo_stack == []
    assert history.active_actions == {}


def test_push_respects_limit_and_changed_optional() -> None:
    history, _applied, _changed = _history(limit=3, with_changed=False)
    for value in ("a", "b", "c", "d"):
        history.push(value)
    assert history.undo_stack == ["b", "c", "d"]  # trimmed to limit


def test_install_shortcut_legend_toggle_and_resize(qapp) -> None:
    qtcore, _qtgui, qtwidgets, _pg = import_qt()
    plot = qtwidgets.QWidget()
    plot.resize(400, 300)
    rows = (("Click", "select"), ("Drag", "move"), ("Up", "clock"), ("Ctrl+Z", "undo"))

    overlay, legend_filter = install_curve_editor_shortcut_legend(
        QtWidgets=qtwidgets,
        QtCore=qtcore,
        plot_widget=plot,
        parent=plot,
        rows=rows,
    )
    assert overlay is not None and legend_filter is not None

    toggle = overlay.findChild(qtwidgets.QToolButton)
    toggle.click()  # collapse
    toggle.click()  # expand
    # A resize routes through the installed event filter -> position_legend.
    plot.resize(520, 360)
    qtwidgets.QApplication.processEvents()
