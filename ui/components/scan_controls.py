from __future__ import annotations

from ui.features.tuning.tuning import AUTO_UV_PRESET_ADAPTIVE
from ui.features.tuning.tuning import DEFAULT_AUTO_UV_PRESET
from ui.features.tuning.tuning import auto_uv_preset
from ui.features.tuning.tuning import auto_uv_presets
from ui.features.tuning.tuning import auto_uv_scan_estimate_minutes
from ui.features.tuning.tuning import auto_uv_scan_target_description


class ScanControls:
    def __init__(self, *, QtWidgets):
        self.widget = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(self.widget)
        layout.setContentsMargins(0, 0, 0, 0)
        self.import_afterburner_button = QtWidgets.QPushButton("Import Afterburner")
        self.import_afterburner_button.setObjectName("importAfterburnerButton")
        self.import_afterburner_button.setIcon(
            self.widget.style().standardIcon(
                getattr(
                    getattr(QtWidgets.QStyle, "StandardPixmap", QtWidgets.QStyle),
                    "SP_DialogOpenButton",
                )
            )
        )
        self.about_button = QtWidgets.QPushButton("About")
        self.about_button.setObjectName("aboutButton")
        self.about_button.setIcon(
            self.widget.style().standardIcon(
                getattr(
                    getattr(QtWidgets.QStyle, "StandardPixmap", QtWidgets.QStyle),
                    "SP_MessageBoxInformation",
                )
            )
        )
        self.stop_button = QtWidgets.QPushButton("Stop")
        self.stop_button.setObjectName("stopButton")
        self.stop_button.setEnabled(False)
        self.status_label = QtWidgets.QLabel(
            "Auto-UV profiles are stored automatically in the main profile store."
        )
        self.dependency_progress = QtWidgets.QProgressBar()
        self.dependency_progress.setObjectName("dependencyProgress")
        self.dependency_progress.setRange(0, 100)
        self.dependency_progress.setValue(0)
        self.dependency_progress.setTextVisible(True)
        self.dependency_progress.setFormat("Downloading dependencies 0%")
        self.dependency_progress.setFixedHeight(20)
        self.dependency_progress.setMinimumWidth(260)
        self.dependency_progress.hide()
        layout.addWidget(self.status_label, 1)
        layout.addWidget(self.dependency_progress)
        layout.addWidget(self.stop_button)
        layout.addWidget(self.import_afterburner_button)
        layout.addWidget(self.about_button)

        self.scan_target_widget = QtWidgets.QGroupBox("Choose Auto-UV scan")
        self.scan_target_widget.setObjectName("autoUvScanTargetGroup")
        target_layout = QtWidgets.QVBoxLayout(self.scan_target_widget)
        target_layout.setContentsMargins(12, 18, 12, 10)
        target_layout.setSpacing(7)

        choice_row = QtWidgets.QHBoxLayout()
        choice_row.setContentsMargins(0, 0, 0, 0)
        choice_row.setSpacing(0)
        self.scan_target_button_group = QtWidgets.QButtonGroup(self.scan_target_widget)
        self.scan_target_button_group.setExclusive(True)
        self.scan_target_buttons = {}
        for preset in auto_uv_presets():
            minimum, maximum = auto_uv_scan_estimate_minutes(preset.preset_id)
            label = (
                "Full scan (3 tiers)"
                if preset.preset_id == AUTO_UV_PRESET_ADAPTIVE
                else preset.label
            )
            button = QtWidgets.QPushButton(
                f"{label}\n~{minimum}-{maximum} min scan"
            )
            button.setObjectName("autoUvPresetButton")
            button.setCheckable(True)
            button.setAutoDefault(False)
            button.setDefault(False)
            button.setProperty("presetId", preset.preset_id)
            button.setToolTip(auto_uv_scan_target_description(preset.preset_id))
            button.setToolTipDuration(20000)
            self.scan_target_button_group.addButton(button)
            self.scan_target_buttons[preset.preset_id] = button
            choice_row.addWidget(button, 1)

        self.start_button = QtWidgets.QPushButton("Set Up && Start Auto-UV")
        self.start_button.setObjectName("startAutoUvButton")
        choice_row.addSpacing(12)
        choice_row.addWidget(self.start_button)
        target_layout.addLayout(choice_row)

        self.scan_target_description = QtWidgets.QLabel()
        self.scan_target_description.setObjectName("autoUvScanEstimate")
        self.scan_target_description.setWordWrap(True)
        target_layout.addWidget(self.scan_target_description)
        for preset_id, button in self.scan_target_buttons.items():
            button.toggled.connect(
                lambda checked, selected=preset_id: (
                    self._sync_scan_target_description(selected) if checked else None
                )
            )
        self.set_selected_scan_preset(DEFAULT_AUTO_UV_PRESET)

    def selected_scan_preset(self) -> str:
        checked = self.scan_target_button_group.checkedButton()
        if checked is None:
            return DEFAULT_AUTO_UV_PRESET
        return auto_uv_preset(checked.property("presetId")).preset_id

    def set_selected_scan_preset(self, preset_id: object) -> None:
        selected = auto_uv_preset(preset_id).preset_id
        button = self.scan_target_buttons.get(selected)
        if button is None:
            button = self.scan_target_buttons[DEFAULT_AUTO_UV_PRESET]
        button.setChecked(True)
        self._sync_scan_target_description(selected)

    def _sync_scan_target_description(self, preset_id: object) -> None:
        self.scan_target_description.setText(
            auto_uv_scan_target_description(preset_id)
            + " Actual time varies with the GPU and stability retries."
        )

    def set_status_text(self, text: str) -> None:
        self.status_label.setText(str(text))

    def set_dependency_progress(self, percent, *, detail: str = "") -> None:
        self.set_progress(
            "Downloading dependencies",
            percent,
            detail=detail or "Downloading dependencies",
        )

    def set_verify_progress(
        self,
        percent,
        *,
        elapsed_s=None,
        target_s=None,
        detail: str = "",
    ) -> None:
        text = None
        if elapsed_s is not None and target_s is not None:
            shown_elapsed_s = _clamped_elapsed_s(elapsed_s, target_s)
            text = (
                "Verifying profile "
                f"{_format_duration_compact(shown_elapsed_s)} / "
                f"{_format_duration_compact(target_s)}"
            )
        self.set_progress(
            "Verifying profile",
            percent,
            detail=detail or "Verifying profile",
            text=text,
        )

    def set_progress(
        self,
        label: str,
        percent,
        *,
        detail: str = "",
        text: str | None = None,
    ) -> None:
        try:
            value = int(round(float(percent)))
        except (TypeError, ValueError):
            value = 0
        value = max(0, min(100, value))
        self.dependency_progress.setValue(value)
        self.dependency_progress.setFormat(text or f"{label} {value}%")
        self.dependency_progress.setToolTip(str(detail or label))
        self.dependency_progress.show()

    def hide_dependency_progress(self) -> None:
        self.dependency_progress.hide()
        self.dependency_progress.setValue(0)
        self.dependency_progress.setFormat("Downloading dependencies 0%")

    def set_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running)
        for button in self.scan_target_buttons.values():
            button.setEnabled(not running)
        self.import_afterburner_button.setEnabled(not running)
        self.stop_button.setEnabled(bool(running))


def _format_duration_compact(seconds) -> str:
    try:
        total_s = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        total_s = 0
    if total_s < 60:
        return f"{total_s}s"
    minutes = total_s // 60
    remaining_s = total_s % 60
    if minutes < 60:
        if remaining_s:
            return f"{minutes}min {remaining_s}s"
        return f"{minutes}min"
    hours = minutes // 60
    remaining_min = minutes % 60
    if remaining_min:
        return f"{hours}h {remaining_min}min"
    return f"{hours}h"


def _clamped_elapsed_s(elapsed_s, target_s) -> float:
    try:
        elapsed = max(0.0, float(elapsed_s))
        target = max(0.0, float(target_s))
    except (TypeError, ValueError):
        return 0.0
    if target > 0.0:
        return min(elapsed, target)
    return elapsed
