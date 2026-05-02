from __future__ import annotations

from ..assets import application_version
from ..assets import asset_image_path
from ..constants import APP_DISPLAY_NAME
from ..tuning import GPU_UNDERVOLTING_PURPOSE_TEXT
from .error_details import qt_flags
from .error_details import selectable_text_flags


SPONSOR_URL = "https://github.com/sponsors/jpietek"
ISSUES_URL = "https://github.com/jpietek/PenguinBurner/issues"
ABOUT_LINKS_HTML = (
    "If you like the tool please consider supporting me on Github!<br>"
    f'<a href="{SPONSOR_URL}">{SPONSOR_URL}</a><br><br>'
    "Having issues with PenguinBurner? Please report the bugs here:<br>"
    f'<a href="{ISSUES_URL}">{ISSUES_URL}</a>'
)


def show_about_dialog(*, QtCore, QtGui, QtWidgets, parent) -> None:
    dialog = QtWidgets.QDialog(parent)
    dialog.setWindowTitle("About")
    dialog.setMinimumWidth(520)
    layout = QtWidgets.QVBoxLayout(dialog)
    layout.setContentsMargins(24, 24, 24, 18)
    layout.setSpacing(12)

    logo_label = QtWidgets.QLabel()
    logo_label.setAlignment(QtCore.Qt.AlignCenter)
    icon_path = asset_image_path("penguin-burner.png")
    pixmap = QtGui.QPixmap(str(icon_path)) if icon_path is not None else None
    if pixmap is not None and not pixmap.isNull():
        logo_label.setPixmap(
            pixmap.scaled(
                180,
                180,
                _aspect_mode(QtCore),
                _transform_mode(QtCore),
            )
        )
    layout.addWidget(logo_label)

    title = QtWidgets.QLabel(APP_DISPLAY_NAME)
    title.setAlignment(QtCore.Qt.AlignCenter)
    title.setObjectName("aboutTitle")
    title.setTextInteractionFlags(selectable_text_flags(QtCore))
    layout.addWidget(title)

    version_label = QtWidgets.QLabel(f"Version {application_version()}")
    version_label.setAlignment(QtCore.Qt.AlignCenter)
    version_label.setObjectName("aboutVersion")
    version_label.setTextInteractionFlags(selectable_text_flags(QtCore))
    layout.addWidget(version_label)

    purpose = QtWidgets.QLabel(GPU_UNDERVOLTING_PURPOSE_TEXT)
    purpose.setObjectName("purposeText")
    purpose.setWordWrap(True)
    purpose.setAlignment(QtCore.Qt.AlignCenter)
    purpose.setTextInteractionFlags(selectable_text_flags(QtCore))
    layout.addWidget(purpose)

    body = QtWidgets.QLabel(ABOUT_LINKS_HTML)
    body.setAlignment(QtCore.Qt.AlignCenter)
    body.setWordWrap(True)
    body.setOpenExternalLinks(True)
    body.setTextInteractionFlags(
        qt_flags(
            QtCore.Qt,
            "TextInteractionFlag",
            "TextBrowserInteraction",
            "TextSelectableByMouse",
            "TextSelectableByKeyboard",
            "LinksAccessibleByKeyboard",
        )
    )
    layout.addWidget(body)

    buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok)
    buttons.accepted.connect(dialog.accept)
    layout.addWidget(buttons)
    dialog.exec()


def _aspect_mode(QtCore):
    return getattr(
        getattr(QtCore.Qt, "AspectRatioMode", QtCore.Qt),
        "KeepAspectRatio",
    )


def _transform_mode(QtCore):
    return getattr(
        getattr(QtCore.Qt, "TransformationMode", QtCore.Qt),
        "SmoothTransformation",
    )
