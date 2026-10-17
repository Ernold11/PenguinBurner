from __future__ import annotations

from integrations.launchers.host_process import start_on_host
from overlay.shortcuts import (
    SHORTCUT_ACTIONS,
    shortcut_command,
    shortcut_instructions,
    shortcut_settings_command,
)


def overlay_shortcuts_dialog(*, QtCore, QtWidgets, parent):
    dialog = QtWidgets.QDialog(parent)
    dialog.setObjectName("overlayShortcutsDialog")
    dialog.setWindowTitle("Overlay keyboard shortcuts")
    dialog.setMinimumWidth(570)
    layout = QtWidgets.QVBoxLayout(dialog)
    intro = QtWidgets.QLabel(
        "Show or hide the overlay without leaving your game. Choose a global "
        "shortcut in your desktop settings, for example Alt+X if it is free. "
        "This works on Wayland and X11, even with PenguinBurner closed."
    )
    intro.setWordWrap(True)
    layout.addWidget(intro)
    instructions = QtWidgets.QLabel(shortcut_instructions())
    instructions.setWordWrap(True)
    layout.addWidget(instructions)

    for index, (label, action) in enumerate(SHORTCUT_ACTIONS):
        layout.addWidget(QtWidgets.QLabel(label))
        row = QtWidgets.QHBoxLayout()
        command = QtWidgets.QLineEdit(shortcut_command(action))
        command.setReadOnly(True)
        command.setObjectName(f"overlayShortcutCommand{index}")
        row.addWidget(command, 1)
        copy = QtWidgets.QPushButton("Copy command")
        copy.setObjectName(f"overlayShortcutCopy{index}")
        copy.clicked.connect(
            lambda _checked=False, field=command: QtWidgets.QApplication.clipboard().setText(
                field.text()
            )
        )
        row.addWidget(copy)
        layout.addLayout(row)

    note = QtWidgets.QLabel(
        "The game must be launched with PenguinBurner's overlay layer. "
        "Visibility changes normally appear within a fraction of a second. "
        "Opening this dialog does not assign or replace any shortcuts."
    )
    note.setWordWrap(True)
    layout.addWidget(note)
    buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
    buttons.rejected.connect(dialog.close)
    settings_command = shortcut_settings_command()
    if settings_command:
        settings = buttons.addButton(
            "Open shortcut settings", QtWidgets.QDialogButtonBox.ActionRole
        )
        settings.setObjectName("overlayShortcutSettings")

        def open_settings():
            if not start_on_host(settings_command):
                note.setText("Could not open shortcut settings. " + shortcut_instructions())

        settings.clicked.connect(open_settings)
    layout.addWidget(buttons)
    dialog.setWindowModality(QtCore.Qt.WindowModal)
    return dialog
