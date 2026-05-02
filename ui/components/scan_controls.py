from __future__ import annotations


class ScanControls:
    def __init__(self, *, QtWidgets):
        self.widget = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(self.widget)
        layout.setContentsMargins(0, 0, 0, 0)
        self.start_button = QtWidgets.QPushButton("Setup Auto Undervolt")
        self.start_button.setObjectName("startAutoUvButton")
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
        layout.addWidget(self.start_button)
        layout.addWidget(self.stop_button)
        layout.addWidget(self.import_afterburner_button)
        layout.addWidget(self.about_button)

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
