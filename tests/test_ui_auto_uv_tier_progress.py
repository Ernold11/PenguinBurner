from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ui.components.auto_uv_tier_progress import AutoUvTierProgress
from ui.qt import import_qt


def test_full_scan_tier_progress_advances_in_chronological_order(qapp) -> None:
    _qtcore, _qtgui, qtwidgets, _pg = import_qt()
    progress = AutoUvTierProgress(QtWidgets=qtwidgets)

    progress.start()
    assert not progress.widget.isHidden()
    assert progress.state("efficiency") == "pending"
    assert progress.state("balanced") == "pending"
    assert progress.state("performance") == "pending"

    progress.set_active("efficiency")
    assert progress.state("efficiency") == "active"
    assert "Scanning" in progress.steps["efficiency"].text()

    progress.set_completed("efficiency")
    progress.set_active("balanced")
    assert progress.state("efficiency") == "complete"
    assert progress.state("balanced") == "active"
    assert progress.state("performance") == "pending"

    progress.set_completed("balanced")
    progress.set_active("performance")
    assert progress.state("performance") == "active"
    progress.set_completed("performance")
    assert [progress.state(tier) for tier in ("efficiency", "balanced", "performance")] == [
        "complete",
        "complete",
        "complete",
    ]


def test_full_scan_tier_progress_marks_unfinished_profiles(qapp) -> None:
    _qtcore, _qtgui, qtwidgets, _pg = import_qt()
    progress = AutoUvTierProgress(QtWidgets=qtwidgets)
    progress.start()
    progress.set_completed("efficiency")
    progress.set_active("balanced")

    progress.mark_unfinished("failed")

    assert progress.state("efficiency") == "complete"
    assert progress.state("balanced") == "failed"
    assert progress.state("performance") == "not-run"

