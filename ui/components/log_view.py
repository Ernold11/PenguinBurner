from __future__ import annotations

from datetime import datetime

from common.cli_output import CLI_OUTPUT_WRAP_COLUMNS, wrap_cli_output_text

from .. import theme


class LogView:
    def __init__(self, *, QtCore, QtGui, QtWidgets):
        self.QtCore = QtCore
        self.QtGui = QtGui
        self.QtWidgets = QtWidgets
        self._at_line_start = True
        self.widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(self.widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        tools = QtWidgets.QHBoxLayout()
        tools.setContentsMargins(0, 0, 0, 0)
        tools.addStretch(1)
        self.copy_button = QtWidgets.QToolButton()
        self.copy_button.setText("Copy logs")
        self.copy_button.setToolTip("Copy all CLI logs for a GitHub issue")
        self.copy_button.setIcon(_copy_icon(QtCore, QtGui))
        self.copy_button.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.copy_button.setAutoRaise(True)
        self.copy_button.clicked.connect(self.copy_all)
        tools.addWidget(self.copy_button)

        self.text_edit = QtWidgets.QPlainTextEdit()
        self.text_edit.setReadOnly(True)
        self.text_edit.setLineWrapMode(_plain_text_no_wrap_mode(QtWidgets))
        self.text_edit.setMaximumBlockCount(6000)
        self._apply_log_font()

        layout.addLayout(tools)
        layout.addWidget(self.text_edit, 1)

    def append(self, text: str) -> None:
        formatted, self._at_line_start = _timestamp_log_text(
            text,
            timestamp=_log_timestamp(),
            at_line_start=self._at_line_start,
        )
        if not formatted:
            return
        scrollbar = self.text_edit.verticalScrollBar()
        follow_tail = scrollbar.value() >= scrollbar.maximum() - 2
        cursor = self.QtGui.QTextCursor(self.text_edit.document())
        cursor.movePosition(self.QtGui.QTextCursor.End)
        cursor.insertText(formatted)
        if follow_tail:
            scrollbar.setValue(scrollbar.maximum())

    def copy_all(self) -> None:
        self.QtWidgets.QApplication.clipboard().setText(self.text_edit.toPlainText())

    def _apply_log_font(self) -> None:
        font_database_enum = getattr(
            self.QtGui.QFontDatabase,
            "SystemFont",
            self.QtGui.QFontDatabase,
        )
        font = self.QtGui.QFontDatabase.systemFont(
            getattr(font_database_enum, "FixedFont")
        )
        style_hint_enum = getattr(self.QtGui.QFont, "StyleHint", self.QtGui.QFont)
        font.setStyleHint(getattr(style_hint_enum, "Monospace"))
        font.setFixedPitch(True)
        point_size = font.pointSize()
        if point_size > 0:
            font.setPointSize(max(8, min(point_size - 1, 9)))
        else:
            font.setPointSize(9)
        self.text_edit.setFont(font)
        metrics = self.QtGui.QFontMetrics(font)
        self.text_edit.setTabStopDistance(max(1, metrics.horizontalAdvance(" ") * 4))


def _log_timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _timestamp_log_text(
    text: str,
    *,
    timestamp: str,
    at_line_start: bool = True,
) -> tuple[str, bool]:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not normalized:
        return "", bool(at_line_start)
    parts: list[str] = []
    line_start = bool(at_line_start)
    for segment in normalized.splitlines(keepends=True):
        if line_start and segment != "\n":
            segment = f"[{timestamp}] {segment}"
        parts.append(
            wrap_cli_output_text(
                segment,
                width=CLI_OUTPUT_WRAP_COLUMNS,
                preserve_json_documents=False,
                preserve_json_lines=False,
            )
        )
        line_start = segment.endswith("\n")
    return "".join(parts), line_start


def _plain_text_no_wrap_mode(QtWidgets):
    line_wrap_enum = getattr(
        QtWidgets.QPlainTextEdit,
        "LineWrapMode",
        QtWidgets.QPlainTextEdit,
    )
    return getattr(line_wrap_enum, "NoWrap")


def _copy_icon(QtCore, QtGui):
    icon = QtGui.QIcon.fromTheme("edit-copy")
    if not icon.isNull():
        return icon

    pixmap = QtGui.QPixmap(16, 16)
    pixmap.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.Antialiasing)
    pen = QtGui.QPen(QtGui.QColor(theme.TEXT))
    pen.setWidth(1)
    painter.setPen(pen)
    painter.setBrush(QtGui.QColor(theme.CONTROL_BG))
    painter.drawRoundedRect(3, 1, 9, 11, 1.5, 1.5)
    painter.setBrush(QtGui.QColor(theme.BORDER_STRONG))
    painter.drawRoundedRect(6, 4, 9, 11, 1.5, 1.5)
    painter.end()
    return QtGui.QIcon(pixmap)
