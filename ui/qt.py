from __future__ import annotations

from . import theme


def import_qt():
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


def apply_dark_palette(app, QtGui) -> None:
    palette = QtGui.QPalette(app.palette())
    roles = QtGui.QPalette
    colors = {
        roles.Window: theme.WINDOW_BG,
        roles.WindowText: theme.TEXT,
        roles.Base: theme.SURFACE_BG,
        roles.AlternateBase: theme.SURFACE_ALT_BG,
        roles.ToolTipBase: theme.TOOLTIP_BG,
        roles.ToolTipText: theme.TEXT,
        roles.Text: theme.TEXT,
        roles.Button: theme.CONTROL_BG,
        roles.ButtonText: theme.TEXT_STRONG,
        roles.BrightText: theme.ERROR,
        roles.Highlight: theme.PRIMARY_BUTTON_BORDER,
        roles.HighlightedText: theme.WHITE,
        roles.Link: theme.LINK,
    }
    for role, color in colors.items():
        palette.setColor(role, QtGui.QColor(color))
    palette.setColor(roles.Disabled, roles.Text, QtGui.QColor(theme.TEXT_DISABLED))
    palette.setColor(roles.Disabled, roles.ButtonText, QtGui.QColor(theme.TEXT_DISABLED))
    palette.setColor(roles.Disabled, roles.WindowText, QtGui.QColor(theme.TEXT_DISABLED))
    app.setPalette(palette)
