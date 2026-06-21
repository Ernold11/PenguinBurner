from __future__ import annotations

from .dialogs.error_details import process_failure_details
from .dialogs.error_details import qt_enum_name
from .dialogs.error_details import show_error_dialog


class ErrorReporter:
    def __init__(self, *, QtCore, QtGui, QtWidgets, parent, controls, log_view):
        self.QtCore = QtCore
        self.QtGui = QtGui
        self.QtWidgets = QtWidgets
        self.parent = parent
        self.controls = controls
        self.log_view = log_view

    def show(self, title: str, message: str) -> None:
        self.controls.set_status_text(str(message).splitlines()[0])
        self.log_view.append(f"\n{title}: {message}\n")
        show_error_dialog(
            QtCore=self.QtCore,
            QtGui=self.QtGui,
            QtWidgets=self.QtWidgets,
            parent=self.parent,
            title=title,
            message=message,
        )

    def show_process(
        self,
        *,
        title: str,
        action_label: str,
        exit_code,
        exit_status,
    ) -> None:
        details = process_failure_details(
            action_label=action_label,
            exit_code=exit_code,
            exit_status=qt_enum_name(exit_status),
            log_tail=self.recent_log_tail(),
        )
        show_error_dialog(
            QtCore=self.QtCore,
            QtGui=self.QtGui,
            QtWidgets=self.QtWidgets,
            parent=self.parent,
            title=title,
            message=(
                f"{action_label} stopped unexpectedly. "
                "The full error details can be copied for troubleshooting."
            ),
            details=details,
        )

    def recent_log_tail(self, *, lines: int = 80, char_limit: int = 12000) -> str:
        text = self.log_view.text_edit.toPlainText()
        tail = "\n".join(text.splitlines()[-max(1, int(lines)) :])
        return tail[-int(char_limit) :] if len(tail) > int(char_limit) else tail
