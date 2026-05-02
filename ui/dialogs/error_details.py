from __future__ import annotations


def show_error_dialog(
    *,
    QtCore,
    QtGui,
    QtWidgets,
    parent,
    title: str,
    message: str,
    details: str = "",
) -> None:
    dialog = QtWidgets.QDialog(parent)
    dialog.setWindowTitle(str(title))
    dialog.setModal(True)
    dialog.resize(760, 420)

    layout = QtWidgets.QVBoxLayout(dialog)
    header_layout = QtWidgets.QHBoxLayout()
    icon_label = QtWidgets.QLabel()
    icon = critical_error_icon(QtGui, QtWidgets, dialog)
    pixmap = icon.pixmap(48, 48)
    if not pixmap.isNull():
        icon_label.setPixmap(pixmap)
    icon_label.setAlignment(qt_flags(QtCore.Qt, "AlignmentFlag", "AlignTop", "AlignHCenter"))
    message_label = QtWidgets.QLabel(str(message))
    message_label.setWordWrap(True)
    message_label.setTextInteractionFlags(selectable_text_flags(QtCore))
    header_layout.addWidget(icon_label)
    header_layout.addWidget(message_label, 1)
    layout.addLayout(header_layout)

    copy_text = error_dialog_copy_text(title, message, details=details)
    detail_edit = QtWidgets.QPlainTextEdit()
    detail_edit.setReadOnly(True)
    detail_edit.setPlainText(copy_text)
    detail_edit.setLineWrapMode(plain_text_no_wrap_mode(QtWidgets))
    detail_edit.setMinimumHeight(220)
    detail_edit.setFont(fixed_width_font(QtGui))
    layout.addWidget(detail_edit, 1)

    buttons = QtWidgets.QDialogButtonBox(
        QtWidgets.QDialogButtonBox.StandardButton.Ok
    )
    copy_button = buttons.addButton(
        "Copy Error",
        QtWidgets.QDialogButtonBox.ButtonRole.ActionRole,
    )
    copy_button.clicked.connect(
        lambda: QtWidgets.QApplication.clipboard().setText(copy_text)
    )
    buttons.accepted.connect(dialog.accept)
    layout.addWidget(buttons)
    dialog.exec()


def process_failure_details(
    *,
    action_label: str,
    exit_code,
    exit_status: str,
    extra_details: str = "",
    log_tail: str = "",
) -> str:
    lines = [
        f"Action: {str(action_label or '').strip() or 'Unknown action'}",
        f"Exit code: {exit_code}",
        f"Exit status: {str(exit_status or '').strip() or 'Unknown'}",
    ]
    extra_details = str(extra_details or "").strip()
    if extra_details:
        lines.extend(["", extra_details])
    log_tail = str(log_tail or "").strip()
    if log_tail:
        lines.extend(["", "Recent logs:", log_tail])
    return "\n".join(lines)


def qt_enum_name(value) -> str:
    name = getattr(value, "name", None)
    if name:
        return str(name)
    text = str(value or "").strip()
    return text.rsplit(".", 1)[-1] if "." in text else text


def error_dialog_copy_text(title: str, message: str, *, details: str = "") -> str:
    parts = [str(title or "").strip(), "", str(message or "").strip()]
    details = str(details or "").strip()
    if details:
        parts.extend(["", details])
    return "\n".join(part for part in parts if part or part == "")


def selectable_text_flags(QtCore):
    return qt_flags(
        QtCore.Qt,
        "TextInteractionFlag",
        "TextSelectableByMouse",
        "TextSelectableByKeyboard",
    )


def qt_flags(parent, enum_name: str, *names: str):
    enum = getattr(parent, enum_name, parent)
    value = None
    for name in names:
        flag = getattr(enum, name)
        value = flag if value is None else value | flag
    return value


def critical_error_icon(QtGui, QtWidgets, widget):
    icon = QtGui.QIcon.fromTheme("dialog-error")
    if not icon.isNull():
        return icon
    standard_pixmap = getattr(QtWidgets.QStyle, "StandardPixmap", QtWidgets.QStyle)
    return widget.style().standardIcon(getattr(standard_pixmap, "SP_MessageBoxCritical"))


def fixed_width_font(QtGui):
    font_database_enum = getattr(
        QtGui.QFontDatabase,
        "SystemFont",
        QtGui.QFontDatabase,
    )
    font = QtGui.QFontDatabase.systemFont(getattr(font_database_enum, "FixedFont"))
    style_hint_enum = getattr(QtGui.QFont, "StyleHint", QtGui.QFont)
    font.setStyleHint(getattr(style_hint_enum, "Monospace"))
    font.setFixedPitch(True)
    point_size = font.pointSize()
    font.setPointSize(max(8, min(point_size if point_size > 0 else 9, 9)))
    return font


def plain_text_no_wrap_mode(QtWidgets):
    line_wrap_enum = getattr(
        QtWidgets.QPlainTextEdit,
        "LineWrapMode",
        QtWidgets.QPlainTextEdit,
    )
    return getattr(line_wrap_enum, "NoWrap")
