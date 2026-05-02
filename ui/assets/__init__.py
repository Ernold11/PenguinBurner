from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from importlib.resources import files
from pathlib import Path
import re

from ..constants import APP_ICON_NAME


def application_icon(QtGui):
    icon = QtGui.QIcon.fromTheme(APP_ICON_NAME)
    if not icon.isNull():
        return icon
    icon_path = asset_image_path("penguin-burner.png")
    if icon_path is None:
        return QtGui.QIcon()
    return QtGui.QIcon(str(icon_path))


def asset_image_path(filename: str) -> Path | None:
    try:
        path = files("ui.assets").joinpath(filename)
        with path.open("rb"):
            return Path(str(path))
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return None


def application_version() -> str:
    try:
        return package_version("penguin-burner")
    except PackageNotFoundError:
        pass
    try:
        pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
        text = pyproject_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "development"
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, flags=re.MULTILINE)
    return match.group(1) if match else "development"
