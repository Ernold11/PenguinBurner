from __future__ import annotations

from configparser import Error as ConfigParserError
from configparser import RawConfigParser
import os
from pathlib import Path

from . import theme


FLATPAK_INFO_PATH = Path("/.flatpak-info")


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


def prepare_desktop_scale_env(env: dict[str, str] | None = None) -> None:
    env = os.environ if env is None else env
    if not _should_apply_flatpak_kde_settings(env):
        return
    env.setdefault("QT_SCALE_FACTOR_ROUNDING_POLICY", "PassThrough")
    forced_dpi = _read_kde_config_value(
        "kcmfonts",
        "General",
        "forceFontDPI",
    )
    if forced_dpi and forced_dpi != "0":
        env.setdefault("QT_FONT_DPI", forced_dpi)


def apply_desktop_font_settings(app, QtGui, env: dict[str, str] | None = None) -> None:
    env = os.environ if env is None else env
    if not _should_apply_flatpak_kde_settings(env):
        return
    font_text = _read_kde_config_value("kdeglobals", "General", "font")
    if not font_text:
        return
    font = QtGui.QFont()
    if font.fromString(font_text):
        app.setFont(font)


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


def _should_apply_flatpak_kde_settings(env: dict[str, str]) -> bool:
    return FLATPAK_INFO_PATH.is_file() and _desktop_is_kde(env)


def _desktop_is_kde(env: dict[str, str]) -> bool:
    current_desktop = env.get("XDG_CURRENT_DESKTOP", "")
    desktop_parts = current_desktop.replace(";", ":").split(":")
    return env.get("KDE_FULL_SESSION", "").lower() == "true" or any(
        part.strip().lower() == "kde" for part in desktop_parts
    )


def _read_kde_config_value(filename: str, section: str, key: str) -> str:
    path = Path.home() / ".config" / filename
    parser = RawConfigParser(strict=False)
    parser.optionxform = str
    try:
        with path.open("r", encoding="utf-8") as handle:
            parser.read_file(handle)
    except (OSError, ConfigParserError, UnicodeDecodeError):
        return ""
    if not parser.has_section(section):
        return ""
    try:
        return parser.get(section, key, fallback="").strip()
    except ConfigParserError:
        return ""
