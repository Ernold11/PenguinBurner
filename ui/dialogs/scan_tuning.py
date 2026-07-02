from __future__ import annotations

import html

from ..assets import asset_image_path
from ui.features.tuning.gpu_selection import gpu_choices_with_fallback
from ui.features.tuning.tuning import AUTO_UV_PRESET_EFFICIENCY
from ui.features.tuning.tuning import AUTO_UV_PRESET_PERFORMANCE
from ui.features.tuning.tuning import DEFAULT_AUTO_UV_PRESET
from ui.features.tuning.tuning import GPU_UNDERVOLTING_PURPOSE_TEXT
from ui.features.tuning.tuning import auto_uv_clock_drop_default
from ui.features.tuning.tuning import auto_uv_nvml_info_text
from ui.features.tuning.tuning import auto_uv_performance_preset_label
from ui.features.tuning.tuning import auto_uv_performance_preset_tooltip
from ui.features.tuning.tuning import auto_uv_performance_target_default
from ui.features.tuning.tuning import auto_uv_power_limit_default
from ui.features.tuning.tuning import auto_uv_preset
from ui.features.tuning.tuning import auto_uv_presets
from ui.features.tuning.tuning import auto_uv_voltage_drop_default
from ui.features.tuning.tuning import memory_offset_mhz_range
from ui.features.tuning.tuning import read_auto_uv_nvml_info
from .error_details import qt_flags


def select_scan_tuning(
    *,
    QtCore,
    QtGui,
    QtWidgets,
    parent,
    gpu_index: int | None = None,
) -> dict | None:
    dialog = QtWidgets.QDialog(parent)
    dialog.setWindowTitle("Automatic undervolt behavior")
    layout = QtWidgets.QVBoxLayout(dialog)
    layout.setContentsMargins(18, 16, 18, 16)
    layout.setSpacing(8)

    purpose = QtWidgets.QLabel(GPU_UNDERVOLTING_PURPOSE_TEXT)
    purpose.setObjectName("purposeText")
    purpose.setWordWrap(True)
    purpose.setContentsMargins(0, 0, 0, 0)
    purpose.setAlignment(qt_flags(QtCore.Qt, "AlignmentFlag", "AlignLeft", "AlignTop"))

    gpu_choices, selected_gpu_index = gpu_choices_with_fallback(
        selected_index=gpu_index
    )
    voltage_drop_default = auto_uv_voltage_drop_default(gpu_index=selected_gpu_index)
    clock_drop_default = auto_uv_clock_drop_default(
        gpu_index=selected_gpu_index,
        preset_id=DEFAULT_AUTO_UV_PRESET,
    )
    gpu_combo = QtWidgets.QComboBox()
    gpu_combo.setObjectName("gpuSelector")
    gpu_combo.setMinimumWidth(360)
    size_adjust_policy = getattr(
        getattr(QtWidgets.QComboBox, "SizeAdjustPolicy", QtWidgets.QComboBox),
        "AdjustToContents",
    )
    gpu_combo.setSizeAdjustPolicy(size_adjust_policy)
    for choice in gpu_choices:
        gpu_combo.addItem(choice.label, int(choice.index))
    selected_combo_index = _gpu_combo_index(gpu_combo, selected_gpu_index)
    if selected_combo_index >= 0:
        gpu_combo.setCurrentIndex(selected_combo_index)

    gpu_group = QtWidgets.QGroupBox("GPU")
    gpu_group.setObjectName("gpuSelectionGroup")
    gpu_layout = _advanced_form_layout(QtCore=QtCore, QtWidgets=QtWidgets)
    gpu_layout.setContentsMargins(14, 18, 14, 12)
    gpu_group.setLayout(gpu_layout)
    _add_form_row(
        QtCore=QtCore,
        QtWidgets=QtWidgets,
        form_layout=gpu_layout,
        text="Graphics card",
        widget=gpu_combo,
        tooltip=(
            "Select the NVIDIA GPU PenguinBurner should use for the full "
            "Auto-UV scan, Q2RTX stability workload, profile verification, "
            "and runtime profile application."
        ),
    )
    gpu_nvml_info = QtWidgets.QLabel()
    gpu_nvml_info.setObjectName("gpuNvmlInfo")
    gpu_nvml_info.setWordWrap(True)
    gpu_nvml_info.setTextInteractionFlags(
        qt_flags(QtCore.Qt, "TextInteractionFlag", "TextSelectableByMouse")
    )
    gpu_nvml_info.setMinimumWidth(360)
    power_limit_controls = {}

    def sync_gpu_nvml_info() -> None:
        selected = _selected_gpu_index(gpu_combo, selected_gpu_index)
        info = read_auto_uv_nvml_info(selected)
        gpu_nvml_info.setText(auto_uv_nvml_info_text(info))
        power_limit_controls["info"] = info
        _sync_power_limit_controls(power_limit_controls, info)
        sync_power_limit_default()

    gpu_combo.currentIndexChanged.connect(lambda _index: sync_gpu_nvml_info())
    _add_form_row(
        QtCore=QtCore,
        QtWidgets=QtWidgets,
        form_layout=gpu_layout,
        text="NVML limits",
        widget=gpu_nvml_info,
        tooltip=(
            "Read-only values detected from public NVML for the selected GPU. "
            "They are sampled when the dialog opens or the selected GPU changes."
        ),
    )

    preset_group = QtWidgets.QGroupBox("Auto-UV preset")
    preset_group.setObjectName("autoUvPresetGroup")
    preset_layout = QtWidgets.QHBoxLayout(preset_group)
    preset_layout.setContentsMargins(14, 18, 14, 12)
    preset_layout.setSpacing(12)
    preset_layout.addWidget(
        _bias_icon(
            QtCore=QtCore,
            QtGui=QtGui,
            QtWidgets=QtWidgets,
            filename="penguin-burner-green.png",
            tooltip="Efficiency",
        )
    )

    preset_buttons_layout = QtWidgets.QHBoxLayout()
    preset_buttons_layout.setContentsMargins(0, 0, 0, 0)
    preset_buttons_layout.setSpacing(0)
    preset_button_group = QtWidgets.QButtonGroup(dialog)
    preset_button_group.setExclusive(True)
    preset_buttons = {}
    preset_tooltips = {
        "efficiency": (
            "Allow up to 15% max loaded clock drop; prefer best efficiency "
            "FPS per watt."
        ),
        "balanced": (
            "Try to maintain baseline clock while lowering the voltage; "
            "the tail of the curve goes 4 V/F bins up."
        ),
        "performance": auto_uv_performance_preset_tooltip(),
    }
    for preset in auto_uv_presets():
        label = (
            auto_uv_performance_preset_label()
            if preset.preset_id == "performance"
            else preset.label
        )
        button = QtWidgets.QPushButton(label)
        button.setObjectName("autoUvPresetButton")
        button.setCheckable(True)
        button.setAutoDefault(False)
        button.setDefault(False)
        button.setProperty("presetId", preset.preset_id)
        button.setToolTip(_wrapped_tooltip(preset_tooltips[preset.preset_id]))
        button.setToolTipDuration(20000)
        preset_button_group.addButton(button)
        preset_buttons[preset.preset_id] = button
        preset_buttons_layout.addWidget(button)
    preset_buttons[DEFAULT_AUTO_UV_PRESET].setChecked(True)
    preset_layout.addLayout(preset_buttons_layout, 1)
    preset_layout.addWidget(
        _bias_icon(
            QtCore=QtCore,
            QtGui=QtGui,
            QtWidgets=QtWidgets,
            filename="penguin-burner.png",
            tooltip="Performance",
        )
    )

    advanced_group = QtWidgets.QGroupBox("Advanced")
    advanced_group.setObjectName("advancedTuningGroup")
    advanced_layout = QtWidgets.QVBoxLayout(advanced_group)
    advanced_layout.setContentsMargins(18, 28, 18, 16)
    advanced_layout.setSpacing(10)

    preset_advanced_stack = QtWidgets.QStackedWidget()
    preset_advanced_stack.setSizePolicy(
        QtWidgets.QSizePolicy.Policy.Expanding,
        QtWidgets.QSizePolicy.Policy.Fixed,
    )
    advanced_pages = {}

    max_clock_drop_spin = _double_spin(
        QtWidgets,
        1.0,
        30.0,
        float(clock_drop_default.value_pct),
        "%",
    )
    max_clock_drop_spin.setObjectName("maxClockDropSpin")
    voltage_floor_spin = QtWidgets.QSpinBox()
    voltage_floor_spin.setRange(700, 1250)
    voltage_floor_spin.setSuffix(" mV")
    voltage_floor_spin.setSingleStep(5)
    voltage_floor_spin.setFixedWidth(136)
    voltage_floor_spin.setValue(
        int(getattr(voltage_drop_default, "floor_voltage_mv", None) or 850)
    )
    memory_offset_spin = QtWidgets.QSpinBox()
    memory_offset_spin.setObjectName("memoryOffsetSpin")
    # The driver range and the applied offset are NVML transfer-rate units
    # (MT/s); the realized memory clock moves by half. The user picks the memory
    # clock (MHz) here, so the box works in MHz (half the MT/s range) and the
    # equivalent MT/s is shown alongside. Converted back to MT/s on accept.
    memory_min_mt_s, memory_max_mt_s = memory_offset_mhz_range()
    memory_offset_spin.setRange(int(memory_min_mt_s) // 2, int(memory_max_mt_s) // 2)
    memory_offset_spin.setSuffix(" MHz")
    memory_offset_spin.setSingleStep(25)
    memory_offset_spin.setFixedWidth(136)
    memory_offset_clock_label = QtWidgets.QLabel()
    memory_offset_clock_label.setObjectName("memoryOffsetClockLabel")

    def _update_memory_offset_clock_label(value: int) -> None:
        # NVML memory offsets are transfer-rate units; the realized memory clock
        # moves by half the offset (verified on Blackwell, issue #20), so the
        # MT/s offset is twice the memory-clock value the user selects.
        memory_offset_clock_label.setText(f"= +{int(value) * 2} MT/s transfer rate")

    memory_offset_spin.valueChanged.connect(_update_memory_offset_clock_label)
    _update_memory_offset_clock_label(memory_offset_spin.value())
    memory_offset_widget = QtWidgets.QWidget()
    memory_offset_layout = QtWidgets.QHBoxLayout(memory_offset_widget)
    memory_offset_layout.setContentsMargins(0, 0, 0, 0)
    memory_offset_layout.setSpacing(10)
    memory_offset_layout.addWidget(memory_offset_spin)
    memory_offset_layout.addWidget(memory_offset_clock_label)
    memory_offset_layout.addStretch(1)
    power_limit_slider = QtWidgets.QSlider(_horizontal_orientation(QtCore))
    power_limit_slider.setObjectName("powerLimitSlider")
    power_limit_slider.setMinimumWidth(220)
    power_limit_slider.setSingleStep(1)
    power_limit_spin = QtWidgets.QSpinBox()
    power_limit_spin.setObjectName("powerLimitSpin")
    power_limit_spin.setSuffix(" W")
    power_limit_spin.setSingleStep(1)
    power_limit_spin.setFixedWidth(116)
    power_limit_widget = QtWidgets.QWidget()
    power_limit_widget.setMinimumWidth(360)
    power_limit_layout = QtWidgets.QHBoxLayout(power_limit_widget)
    power_limit_layout.setContentsMargins(0, 0, 0, 0)
    power_limit_layout.setSpacing(10)
    power_limit_layout.addWidget(power_limit_slider, 1)
    power_limit_layout.addWidget(power_limit_spin)
    power_limit_slider.valueChanged.connect(power_limit_spin.setValue)
    power_limit_spin.valueChanged.connect(power_limit_slider.setValue)
    power_limit_controls.update(
        {
            "slider": power_limit_slider,
            "spin": power_limit_spin,
        }
    )

    efficiency_page = QtWidgets.QWidget()
    efficiency_form = _advanced_form_layout(QtCore=QtCore, QtWidgets=QtWidgets)
    efficiency_page.setLayout(efficiency_form)
    _add_form_row(
        QtCore=QtCore,
        QtWidgets=QtWidgets,
        form_layout=efficiency_form,
        text="Min voltage",
        widget=voltage_floor_spin,
        tooltip=(
            "Lowest V/F voltage bin Auto-UV may try in Efficiency. The default "
            "comes from PenguinBurner's GPU table when detected; unknown GPUs "
            "use a 10% fallback from the reference voltage."
        ),
    )
    balanced_page = QtWidgets.QWidget()
    balanced_page.setLayout(_advanced_form_layout(QtCore=QtCore, QtWidgets=QtWidgets))

    performance_page = QtWidgets.QWidget()
    performance_form = _advanced_form_layout(QtCore=QtCore, QtWidgets=QtWidgets)
    performance_page.setLayout(performance_form)
    performance_target = auto_uv_performance_target_default(
        gpu_name=voltage_drop_default.gpu_name,
    )
    performance_voltage_spin = QtWidgets.QSpinBox()
    performance_voltage_spin.setObjectName("performanceVoltageSpin")
    performance_voltage_spin.setRange(700, 1250)
    performance_voltage_spin.setSuffix(" mV")
    performance_voltage_spin.setSingleStep(5)
    performance_voltage_spin.setFixedWidth(136)
    performance_voltage_spin.setValue(int(performance_target.voltage_mv or 950))
    performance_clock_spin = QtWidgets.QSpinBox()
    performance_clock_spin.setObjectName("performanceClockSpin")
    performance_clock_spin.setRange(1000, 4000)
    performance_clock_spin.setSuffix(" MHz")
    performance_clock_spin.setSingleStep(15)
    performance_clock_spin.setFixedWidth(136)
    performance_clock_spin.setValue(int(performance_target.clock_mhz or 3000))
    _add_form_row(
        QtCore=QtCore,
        QtWidgets=QtWidgets,
        form_layout=performance_form,
        text="Auto-OC voltage target",
        widget=performance_voltage_spin,
        tooltip="Editable voltage cap for the internal Performance Auto-OC pass.",
    )
    _add_form_row(
        QtCore=QtCore,
        QtWidgets=QtWidgets,
        form_layout=performance_form,
        text="Auto-OC clock target",
        widget=performance_clock_spin,
        tooltip="Editable core clock cap for the internal Performance Auto-OC pass.",
    )

    advanced_pages[AUTO_UV_PRESET_EFFICIENCY] = efficiency_page
    advanced_pages[DEFAULT_AUTO_UV_PRESET] = balanced_page
    advanced_pages[AUTO_UV_PRESET_PERFORMANCE] = performance_page
    for page in advanced_pages.values():
        preset_advanced_stack.addWidget(page)
    preset_advanced_stack.setMinimumHeight(
        max(page.sizeHint().height() for page in advanced_pages.values())
    )

    common_advanced = QtWidgets.QWidget()
    common_form = _advanced_form_layout(QtCore=QtCore, QtWidgets=QtWidgets)
    common_advanced.setLayout(common_form)
    _add_form_row(
        QtCore=QtCore,
        QtWidgets=QtWidgets,
        form_layout=common_form,
        text="Max loaded clock drop",
        widget=max_clock_drop_spin,
        tooltip=(
            "How much loaded core-clock degradation Auto-UV may accept. The "
            "default is preset-aware from the GPU table when detected; unknown "
            "GPUs use a generic fallback."
        ),
    )
    _add_form_row(
        QtCore=QtCore,
        QtWidgets=QtWidgets,
        form_layout=common_form,
        text="Memory Offset",
        widget=memory_offset_widget,
        tooltip=(
            "Optional global memory clock V/F offset applied during the "
            "Auto-UV scan and saved with the final profile. You set the memory "
            "clock rise in MHz; the equivalent NVML transfer-rate offset (MT/s, "
            "like Afterburner/LACT), which is twice the clock, is shown "
            "alongside. The range is read from the driver for this GPU. Higher "
            "values can improve memory performance, but may introduce "
            "instability; modify with care."
        ),
    )
    _add_form_row(
        QtCore=QtCore,
        QtWidgets=QtWidgets,
        form_layout=common_form,
        text="Power limit",
        widget=power_limit_widget,
        tooltip=(
            "Power limit in watts applied during this Auto-UV scan and saved "
            "with the final profile. The range comes from NVML for the selected "
            "GPU; the default position is the card's NVML default limit."
        ),
    )
    advanced_layout.addWidget(preset_advanced_stack)
    advanced_layout.addWidget(common_advanced)

    def checked_preset_id() -> str:
        checked_button = preset_button_group.checkedButton()
        if checked_button is None:
            return DEFAULT_AUTO_UV_PRESET
        return str(checked_button.property("presetId") or DEFAULT_AUTO_UV_PRESET)

    def sync_preset_specific_fields() -> None:
        preset_advanced_stack.setCurrentWidget(
            advanced_pages.get(checked_preset_id(), balanced_page)
        )

    clock_drop_syncing = {"active": False}
    clock_drop_manual_override = {"active": False}

    def mark_clock_drop_manual_override(_value) -> None:
        if not bool(clock_drop_syncing["active"]):
            clock_drop_manual_override["active"] = True

    def sync_clock_drop_default() -> None:
        if bool(clock_drop_manual_override["active"]):
            return
        selected = _selected_gpu_index(gpu_combo, selected_gpu_index)
        default = auto_uv_clock_drop_default(
            gpu_index=selected,
            preset_id=checked_preset_id(),
        )
        clock_drop_syncing["active"] = True
        max_clock_drop_spin.setValue(float(default.value_pct))
        clock_drop_syncing["active"] = False

    power_limit_syncing = {"active": False}
    power_limit_manual_override = {"active": False}

    def mark_power_limit_manual_override(_value) -> None:
        if not bool(power_limit_syncing["active"]):
            power_limit_manual_override["active"] = True

    def sync_power_limit_default() -> None:
        if bool(power_limit_manual_override["active"]):
            return
        spin = power_limit_controls.get("spin")
        slider = power_limit_controls.get("slider")
        info = power_limit_controls.get("info")
        if spin is None or slider is None or not spin.isEnabled():
            return
        selected = _selected_gpu_index(gpu_combo, selected_gpu_index)
        default = auto_uv_power_limit_default(
            max_w=getattr(info, "power_limit_max_w", None),
            min_w=getattr(info, "power_limit_min_w", None),
            default_w=getattr(info, "power_limit_default_w", None),
            gpu_index=selected,
            preset_id=checked_preset_id(),
        )
        if default.watts is None:
            return
        watts = max(spin.minimum(), min(spin.maximum(), int(default.watts)))
        power_limit_syncing["active"] = True
        spin.setValue(watts)
        slider.setValue(watts)
        power_limit_syncing["active"] = False

    def sync_preset_defaults() -> None:
        sync_preset_specific_fields()
        sync_clock_drop_default()
        sync_power_limit_default()

    max_clock_drop_spin.valueChanged.connect(mark_clock_drop_manual_override)
    power_limit_controls["spin"].valueChanged.connect(mark_power_limit_manual_override)
    gpu_combo.currentIndexChanged.connect(lambda _index: sync_clock_drop_default())
    preset_button_group.buttonClicked.connect(lambda _button: sync_preset_defaults())
    sync_preset_specific_fields()
    sync_gpu_nvml_info()

    buttons = QtWidgets.QDialogButtonBox()
    role_enum = getattr(QtWidgets.QDialogButtonBox, "ButtonRole", QtWidgets.QDialogButtonBox)
    standard_enum = getattr(
        QtWidgets.QDialogButtonBox,
        "StandardButton",
        QtWidgets.QDialogButtonBox,
    )
    start_button = buttons.addButton(
        "Start Auto Undervolt",
        getattr(role_enum, "AcceptRole"),
    )
    buttons.addButton(getattr(standard_enum, "Cancel"))
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    start_button.setDefault(True)
    _install_spinbox_enter_commit_filter(
        QtCore=QtCore,
        QtWidgets=QtWidgets,
        parent=dialog,
        spinboxes=[
            max_clock_drop_spin,
            voltage_floor_spin,
            memory_offset_spin,
            power_limit_spin,
            performance_voltage_spin,
            performance_clock_spin,
        ],
    )

    layout.addWidget(purpose)
    layout.addWidget(gpu_group)
    layout.addWidget(preset_group)
    layout.addWidget(advanced_group)
    layout.addWidget(buttons)
    dialog.setMinimumWidth(860)
    dialog.resize(860, dialog.sizeHint().height())
    if dialog.exec() != QtWidgets.QDialog.Accepted:
        return None

    checked_button = preset_button_group.checkedButton()
    preset_id = (
        checked_button.property("presetId")
        if checked_button is not None
        else DEFAULT_AUTO_UV_PRESET
    )
    preset = auto_uv_preset(preset_id)
    options = {
        "gpu_index": _selected_gpu_index(gpu_combo, selected_gpu_index),
        "auto_uv_mode": preset.auto_uv_mode,
        "auto_uv_max_clock_drop_pct": float(max_clock_drop_spin.value()),
        # The box is in memory-clock MHz; the applied NVML offset is transfer
        # rate (MT/s), which is twice the clock delta.
        "auto_uv_memory_offset_mhz": int(memory_offset_spin.value()) * 2,
    }
    if power_limit_spin.isEnabled() and int(power_limit_spin.value()) > 0:
        options["auto_uv_power_limit_w"] = int(power_limit_spin.value())
    if int(preset.tail_rise_bins) > 0:
        options["auto_uv_tail_rise_bins"] = int(preset.tail_rise_bins)
    if preset.preset_id == AUTO_UV_PRESET_EFFICIENCY:
        options["auto_uv_min_voltage_mv"] = int(voltage_floor_spin.value())
    if preset.preset_id == AUTO_UV_PRESET_PERFORMANCE:
        options.update(
            {
                "auto_oc_target_voltage_mv": int(performance_voltage_spin.value()),
                "auto_oc_target_clock_mhz": int(performance_clock_spin.value()),
            }
        )
    return options


def _gpu_combo_index(gpu_combo, gpu_index: int) -> int:
    for index in range(gpu_combo.count()):
        try:
            if int(gpu_combo.itemData(index)) == int(gpu_index):
                return index
        except (TypeError, ValueError):
            continue
    return -1


def _selected_gpu_index(gpu_combo, fallback: int) -> int:
    try:
        return max(0, int(gpu_combo.currentData()))
    except (TypeError, ValueError):
        return max(0, int(fallback))


def _bias_icon(*, QtCore, QtGui, QtWidgets, filename: str, tooltip: str):
    label = QtWidgets.QLabel()
    label.setFixedSize(56, 56)
    label.setAlignment(QtCore.Qt.AlignCenter)
    label.setToolTip(tooltip)
    path = asset_image_path(filename)
    if path is not None:
        pixmap = QtGui.QPixmap(str(path))
        if not pixmap.isNull():
            label.setPixmap(
                pixmap.scaled(
                    54,
                    54,
                    _aspect_mode(QtCore),
                    _transform_mode(QtCore),
                )
            )
    return label


def _add_form_row(
    *,
    QtCore,
    QtWidgets,
    form_layout,
    text: str,
    widget,
    tooltip: str = "",
) -> None:
    label_widget = QtWidgets.QLabel(text)
    if not tooltip:
        form_layout.addRow(label_widget, widget)
        return

    wrapped = _wrapped_tooltip(tooltip)
    label_widget.setToolTip(wrapped)
    widget.setToolTip(wrapped)
    widget.setToolTipDuration(20000)
    label_container = QtWidgets.QWidget()
    label_layout = QtWidgets.QHBoxLayout(label_container)
    label_layout.setContentsMargins(0, 2, 12, 2)
    label_layout.setSpacing(8)
    info_button = QtWidgets.QToolButton()
    info_button.setObjectName("infoButton")
    info_button.setText("i")
    info_button.setToolTip(wrapped)
    info_button.setToolTipDuration(20000)
    info_button.setCursor(QtCore.Qt.WhatsThisCursor)
    info_button.setFocusPolicy(QtCore.Qt.NoFocus)
    info_button.setFixedSize(18, 18)

    def show_tooltip(_checked=False, *, button=info_button, tip=wrapped):
        position = button.mapToGlobal(button.rect().bottomLeft())
        QtWidgets.QToolTip.showText(position, tip, button)

    info_button.clicked.connect(show_tooltip)
    label_layout.addWidget(label_widget)
    label_layout.addWidget(info_button)
    label_layout.addStretch(1)
    form_layout.addRow(label_container, widget)


def _advanced_form_layout(*, QtCore, QtWidgets):
    form = QtWidgets.QFormLayout()
    form.setContentsMargins(0, 0, 0, 0)
    form.setHorizontalSpacing(24)
    form.setVerticalSpacing(10)
    form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldsStayAtSizeHint)
    form.setFormAlignment(qt_flags(QtCore.Qt, "AlignmentFlag", "AlignLeft", "AlignTop"))
    form.setLabelAlignment(qt_flags(QtCore.Qt, "AlignmentFlag", "AlignLeft", "AlignVCenter"))
    return form


def _wrapped_tooltip(text: str) -> str:
    normalized = " ".join(str(text).split())
    escaped = html.escape(normalized)
    return f"<qt><table width='680'><tr><td>{escaped}</td></tr></table></qt>"


def _double_spin(QtWidgets, minimum: float, maximum: float, value: float, suffix: str):
    spin = QtWidgets.QDoubleSpinBox()
    spin.setRange(float(minimum), float(maximum))
    spin.setDecimals(1)
    spin.setSuffix(str(suffix))
    spin.setFixedWidth(116)
    spin.setValue(float(value))
    return spin


def _install_spinbox_enter_commit_filter(*, QtCore, QtWidgets, parent, spinboxes) -> None:
    event_type = getattr(getattr(QtCore.QEvent, "Type", QtCore.QEvent), "KeyPress")
    key_enum = getattr(QtCore.Qt, "Key", QtCore.Qt)
    enter_keys = {
        _qt_enum_value(getattr(key_enum, "Key_Return")),
        _qt_enum_value(getattr(key_enum, "Key_Enter")),
    }

    class _SpinBoxEnterFilter(QtCore.QObject):
        def __init__(self):
            super().__init__(parent)
            self._spinboxes_by_target = {}
            for spinbox in spinboxes:
                self._spinboxes_by_target[spinbox] = spinbox
                try:
                    editor = spinbox.lineEdit()
                except AttributeError:
                    editor = None
                if editor is not None:
                    self._spinboxes_by_target[editor] = spinbox

        def eventFilter(self, watched, event):  # noqa: N802 - Qt override name
            if watched not in self._spinboxes_by_target:
                return False
            if event.type() != event_type:
                return False
            try:
                key = _qt_enum_value(event.key())
            except AttributeError:
                return False
            if key not in enter_keys:
                return False
            spinbox = self._spinboxes_by_target[watched]
            spinbox.interpretText()
            event.accept()
            return True

    event_filter = _SpinBoxEnterFilter()
    for target in event_filter._spinboxes_by_target:
        target.installEventFilter(event_filter)
    parent._penguin_burner_spinbox_enter_filter = event_filter


def _qt_enum_value(value) -> int:
    return int(getattr(value, "value", value))


def _aspect_mode(QtCore):
    return getattr(
        getattr(QtCore.Qt, "AspectRatioMode", QtCore.Qt),
        "KeepAspectRatio",
    )


def _transform_mode(QtCore):
    return getattr(
        getattr(QtCore.Qt, "TransformationMode", QtCore.Qt),
        "SmoothTransformation",
    )


def _horizontal_orientation(QtCore):
    return getattr(
        getattr(QtCore.Qt, "Orientation", QtCore.Qt),
        "Horizontal",
    )


def _sync_power_limit_controls(controls: dict, info) -> None:
    slider = controls.get("slider")
    spin = controls.get("spin")
    if slider is None or spin is None:
        return

    values = _power_limit_control_values(info)
    if values is None:
        slider.blockSignals(True)
        spin.blockSignals(True)
        slider.setRange(0, 0)
        spin.setRange(0, 0)
        slider.setValue(0)
        spin.setValue(0)
        slider.setEnabled(False)
        spin.setEnabled(False)
        spin.blockSignals(False)
        slider.blockSignals(False)
        return

    min_w, max_w, default_w = values
    page_step = max(1, int(round((max_w - min_w) / 6.0)))
    slider.blockSignals(True)
    spin.blockSignals(True)
    slider.setRange(min_w, max_w)
    spin.setRange(min_w, max_w)
    slider.setPageStep(page_step)
    spin.setSingleStep(1)
    slider.setValue(default_w)
    spin.setValue(default_w)
    slider.setEnabled(True)
    spin.setEnabled(True)
    spin.blockSignals(False)
    slider.blockSignals(False)


def _power_limit_control_values(info) -> tuple[int, int, int] | None:
    if info is None:
        return None
    min_w = _positive_rounded_int(getattr(info, "power_limit_min_w", None))
    max_w = _positive_rounded_int(getattr(info, "power_limit_max_w", None))
    if min_w is None or max_w is None or max_w < min_w:
        return None
    default_w = _positive_rounded_int(getattr(info, "power_limit_default_w", None))
    if default_w is None:
        default_w = _positive_rounded_int(getattr(info, "power_limit_w", None))
    if default_w is None:
        default_w = int(round((min_w + max_w) / 2.0))
    default_w = max(min_w, min(max_w, default_w))
    return min_w, max_w, default_w


def _positive_rounded_int(value) -> int | None:
    if value is None:
        return None
    try:
        rounded = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return rounded if rounded > 0 else None
