from __future__ import annotations

from collections.abc import Callable


def open_memory_offset_editor_dialog(
    *,
    QtWidgets,
    parent,
    current_memory_offset_mhz: int,
    min_mhz: int,
    max_mhz: int,
    save_callback: Callable[[int], str | None],
) -> bool:
    dialog = QtWidgets.QDialog(parent)
    dialog.setWindowTitle("Edit Memory Offset")
    layout = QtWidgets.QVBoxLayout(dialog)

    form = QtWidgets.QFormLayout()
    layout.addLayout(form)

    # NVML applies memory offsets in transfer-rate units (MT/s), but the
    # realized memory clock moves by half (verified on Blackwell, issue
    # #20), so the spin box works in clock MHz and the stored/applied value
    # is twice that.
    memory_spin = QtWidgets.QSpinBox()
    memory_spin.setObjectName("memoryOffsetEditSpin")
    memory_spin.setSuffix(" MHz")
    memory_spin.setSingleStep(25)
    memory_spin.setFixedWidth(136)
    memory_spin.setRange(int(min_mhz) // 2, max(int(min_mhz) // 2, int(max_mhz) // 2))
    memory_spin.setValue(
        max(
            memory_spin.minimum(),
            min(memory_spin.maximum(), int(current_memory_offset_mhz) // 2),
        )
    )

    memory_clock_label = QtWidgets.QLabel()
    memory_clock_label.setObjectName("memoryOffsetEditClockLabel")

    def update_memory_label(value: int) -> None:
        memory_clock_label.setText(f"= {int(value) * 2:+d} MT/s transfer rate")

    memory_spin.valueChanged.connect(update_memory_label)
    update_memory_label(memory_spin.value())

    memory_widget = QtWidgets.QWidget()
    memory_layout = QtWidgets.QHBoxLayout(memory_widget)
    memory_layout.setContentsMargins(0, 0, 0, 0)
    memory_layout.setSpacing(10)
    memory_layout.addWidget(memory_spin)
    memory_layout.addWidget(memory_clock_label)
    memory_layout.addStretch(1)
    form.addRow("Memory Offset", memory_widget)

    status_label = QtWidgets.QLabel()
    layout.addWidget(status_label)

    button_row = QtWidgets.QHBoxLayout()
    button_row.addStretch(1)
    save_button = QtWidgets.QPushButton("Save")
    cancel_button = QtWidgets.QPushButton("Cancel")
    button_row.addWidget(save_button)
    button_row.addWidget(cancel_button)
    layout.addLayout(button_row)

    def save_edit() -> None:
        try:
            message = save_callback(int(memory_spin.value()) * 2)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(
                dialog,
                "Edit Memory Offset",
                f"Failed to save edited memory offset: {exc}",
            )
            return
        if message:
            status_label.setText(str(message))
        dialog.accept()

    save_button.clicked.connect(save_edit)
    cancel_button.clicked.connect(dialog.reject)
    return bool(dialog.exec() == QtWidgets.QDialog.Accepted)
