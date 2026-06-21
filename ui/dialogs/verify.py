from __future__ import annotations

from ui.features.tuning.verify import DEFAULT_VERIFY_DURATION_S
from ui.features.tuning.verify import MAX_VERIFY_DURATION_S


def select_verify_options(*, QtWidgets, parent, profile_label: str) -> dict | None:
    dialog = QtWidgets.QDialog(parent)
    dialog.setWindowTitle("Verify Profile")
    layout = QtWidgets.QVBoxLayout(dialog)
    intro = QtWidgets.QLabel(f"Verify {profile_label or 'the selected profile'}.")
    intro.setWordWrap(True)

    duration_spin = QtWidgets.QSpinBox()
    duration_spin.setRange(1, MAX_VERIFY_DURATION_S // 60)
    duration_spin.setSuffix(" min")
    duration_spin.setValue(max(1, DEFAULT_VERIFY_DURATION_S // 60))

    duration = QtWidgets.QHBoxLayout()
    duration.addWidget(QtWidgets.QLabel("Verification duration"))
    duration.addWidget(duration_spin)
    duration.addStretch(1)

    buttons = QtWidgets.QDialogButtonBox()
    role_enum = getattr(QtWidgets.QDialogButtonBox, "ButtonRole", QtWidgets.QDialogButtonBox)
    standard_enum = getattr(
        QtWidgets.QDialogButtonBox,
        "StandardButton",
        QtWidgets.QDialogButtonBox,
    )
    start_button = buttons.addButton("Start Verification", getattr(role_enum, "AcceptRole"))
    buttons.addButton(getattr(standard_enum, "Cancel"))
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    start_button.setDefault(True)

    layout.addWidget(intro)
    layout.addLayout(duration)
    layout.addWidget(buttons)
    dialog.setMinimumWidth(420)
    if dialog.exec() != QtWidgets.QDialog.Accepted:
        return None
    return {
        "duration_s": max(60, int(duration_spin.value()) * 60),
    }
