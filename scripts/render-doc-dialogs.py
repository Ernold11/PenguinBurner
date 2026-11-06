#!/usr/bin/env python3
"""Capture the real curve/import dialogs with fixed examples and no writes."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_SCALE_FACTOR", "2")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ui.components.fan_curve_editor import open_fan_curve_editor_dialog  # noqa: E402
from ui.components.vf_curve_editor import open_vf_curve_editor_dialog  # noqa: E402
from ui.dialogs import afterburner_import  # noqa: E402
from ui.qt import apply_dark_palette, import_qt  # noqa: E402
from ui.styles import STYLESHEET  # noqa: E402


def main() -> None:
    QtCore, QtGui, QtWidgets, pg = import_qt()
    app = QtWidgets.QApplication([])
    app.setFont(QtGui.QFont("Noto Sans", 10))
    apply_dark_palette(app, QtGui)
    app.setStyleSheet(STYLESHEET)
    output = ROOT / "docs/assets"
    output.mkdir(parents=True, exist_ok=True)
    names = {
        "Edit VF Curve": "vf-curve-editor.png",
        "Edit Fan Curve": "fan-curve-editor.png",
        "Import Afterburner": "afterburner-import.png",
    }

    def capture(dialog):
        if dialog.windowTitle() == "Import Afterburner":
            dialog.resize(1600, 600)
            dialog.findChild(QtWidgets.QSplitter).setSizes([850, 730])
        dialog.show()
        app.processEvents()
        path = output / names[dialog.windowTitle()]
        if not dialog.grab().save(str(path)):
            raise RuntimeError(f"Could not save {path}")
        print(path)
        dialog.hide()
        dialog.deleteLater()
        return QtWidgets.QDialog.Rejected

    base: list[tuple[float, float]] = [
        (v, min(3100, 2000 + (v - 800) * 4.4)) for v in range(800, 1101, 25)
    ]
    plan = [
        {"index": i, "voltage_mv": v, "base_mhz": clock,
         "target_mhz": min(3000, clock + 340)}
        for i, (v, clock) in enumerate(base)
    ]
    entries = [{
        "device_profile_name": "NVIDIA GeForce RTX 5080.cfg",
        "section": "Profile1", "target": "950 mV / 3000 MHz",
        "status": "Ready to import", "importable": True,
        "target_voltage_mv": 950, "target_clock_mhz": 3000,
        "curve_points": [(p["voltage_mv"], p["target_mhz"]) for p in plan],
    }]
    with patch.object(QtWidgets.QDialog, "exec", capture):
        open_vf_curve_editor_dialog(
            QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets, pg=pg, parent=None,
            plan=plan, base_points=base, anchor=(950, 3000),
            save_callback=lambda _edit: None,
        )
        open_fan_curve_editor_dialog(
            QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets, pg=pg, parent=None,
            curve_points=[(35, 0), (45, 25), (55, 35), (65, 50), (75, 70), (85, 100)],
            measured_points=[(40, 0), (50, 30), (60, 40), (70, 60), (80, 85)],
            target_point=(65, 50), save_callback=lambda _edit: None,
        )
        with (
            patch.object(afterburner_import, "configured_afterburner_root",
                         return_value="/path/to/MSI Afterburner"),
            patch.object(afterburner_import, "afterburner_profile_entries",
                         return_value=entries),
        ):
            afterburner_import.select_afterburner_import(
                QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets, pg=pg, parent=None,
            )
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)


if __name__ == "__main__":
    main()
