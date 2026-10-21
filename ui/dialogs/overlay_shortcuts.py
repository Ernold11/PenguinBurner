from __future__ import annotations

from integrations.launchers.host_process import start_on_host
from overlay.shortcuts import (
    SHORTCUT_ACTIONS,
    SHORTCUT_DEFAULTS,
    shortcut_command,
    shortcut_desktop,
    shortcut_instructions,
    shortcut_settings_command,
)


def overlay_shortcuts_dialog(*, QtCore, QtWidgets, parent, error=""):
    kde = shortcut_desktop() == "kde"
    dialog = QtWidgets.QDialog(parent)
    dialog.setObjectName("overlayShortcutsDialog")
    dialog.setWindowTitle("Overlay keyboard shortcuts")
    dialog.setMinimumWidth(570)
    dialog.resize(680, 440 if kde else 640)
    outer_layout = QtWidgets.QVBoxLayout(dialog)
    scroll = QtWidgets.QScrollArea()
    scroll.setObjectName("overlayShortcutScroll")
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
    content = QtWidgets.QWidget()
    layout = QtWidgets.QVBoxLayout(content)
    layout.setSizeConstraint(QtWidgets.QLayout.SetMinimumSize)
    scroll.setWidget(content)
    outer_layout.addWidget(scroll)
    if error:
        error_label = QtWidgets.QLabel(error)
        error_label.setObjectName("overlayShortcutError")
        error_label.setWordWrap(True)
        layout.addWidget(error_label)
    intro = QtWidgets.QLabel(
        "Alt+X shows or hides the overlay. "
        "Alt+C cycles Small → Medium → Large → Biggest → Small. "
        "Alt+[ / Alt+] changes Scale; Alt+< / Alt+> moves the overlay. "
        "You can change any binding in your desktop settings. "
        "This works on Wayland and X11, even with PenguinBurner closed."
    )
    intro.setWordWrap(True)
    layout.addWidget(intro)
    instructions = QtWidgets.QLabel(shortcut_instructions())
    instructions.setWordWrap(True)
    layout.addWidget(instructions)

    for index, (label, action) in enumerate(SHORTCUT_ACTIONS):
        if action in SHORTCUT_DEFAULTS:
            label = f"{label} (default: {SHORTCUT_DEFAULTS[action]})"
        elif kde:
            label = f"{label} (unassigned)"
        layout.addWidget(QtWidgets.QLabel(label))
        if kde:
            continue
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
        "Changes appear live. Scale stops at 50% and 200%; positions wrap. "
        "Custom fields remain available in the Preset selector. "
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
    outer_layout.addWidget(buttons)
    dialog.setWindowModality(QtCore.Qt.WindowModal)
    return dialog
