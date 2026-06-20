from __future__ import annotations

from ..verify import DEFAULT_VERIFY_DURATION_S
from ..verify import MAX_VERIFY_DURATION_S


def select_verify_options(*, QtWidgets, parent, profile_label: str) -> dict | None:
    dialog = QtWidgets.QDialog(parent)
    dialog.setWindowTitle("Verify Profile")
    layout = QtWidgets.QVBoxLayout(dialog)
    intro = QtWidgets.QLabel(f"Verify {profile_label or 'the selected profile'}.")
    intro.setWordWrap(True)
    q2rtx_checkbox = QtWidgets.QCheckBox("Q2RTX benchmark")
    q2rtx_checkbox.setChecked(True)
    cuda_checkbox = QtWidgets.QCheckBox("CUDA compute test")
    cuda_checkbox.setChecked(True)

    def keep_one_checked(changed_checkbox) -> None:
        if q2rtx_checkbox.isChecked() or cuda_checkbox.isChecked():
            return
        changed_checkbox.setChecked(True)

    q2rtx_checkbox.toggled.connect(lambda _checked: keep_one_checked(q2rtx_checkbox))
    cuda_checkbox.toggled.connect(lambda _checked: keep_one_checked(cuda_checkbox))

    duration_spin = QtWidgets.QSpinBox()
    duration_spin.setRange(1, MAX_VERIFY_DURATION_S // 60)
    duration_spin.setSuffix(" min")
    duration_spin.setValue(max(1, DEFAULT_VERIFY_DURATION_S // 60))

    workloads = QtWidgets.QHBoxLayout()
    workloads.addWidget(QtWidgets.QLabel("Workloads"))
    workloads.addWidget(q2rtx_checkbox)
    workloads.addWidget(cuda_checkbox)
    workloads.addStretch(1)
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
    layout.addLayout(workloads)
    layout.addLayout(duration)
    layout.addWidget(buttons)
    dialog.setMinimumWidth(420)
    if dialog.exec() != QtWidgets.QDialog.Accepted:
        return None
    return {
        "duration_s": max(60, int(duration_spin.value()) * 60),
        "q2rtx_enabled": q2rtx_checkbox.isChecked(),
        "cuda_enabled": cuda_checkbox.isChecked(),
    }
