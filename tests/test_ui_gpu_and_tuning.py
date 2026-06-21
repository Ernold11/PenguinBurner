"""Coverage for ui/gpu_selection.py and ui/tuning.py.

gpu_selection parsing/fallback is pure; the NVML-backed tuning helpers are
exercised with a fake NvmlGpuPolicyController so no real GPU is required.
"""

from __future__ import annotations

from types import SimpleNamespace

import ui.features.tuning.gpu_selection as gpu_selection
import ui.features.tuning.tuning as tuning
from ui.features.tuning.gpu_selection import (
    GpuChoice,
    gpu_choices_with_fallback,
    gpu_choices_from_nvml_identities,
    persist_runtime_gpu_index,
    runtime_gpu_index,
)
from ui.features.tuning.tuning import (
    AutoUvNvmlInfo,
    auto_uv_nvml_info_text,
    auto_uv_performance_preset_label,
    auto_uv_performance_preset_tooltip,
    auto_uv_performance_target_default,
    memory_offset_mhz_range,
)


# --- ui/gpu_selection.py ------------------------------------------------------


def test_gpu_choice_label_with_and_without_bus() -> None:
    assert GpuChoice(0, "RTX 4090", "00000000:01:00.0").label == (
        "GPU 0 - RTX 4090 (01:00.0)"
    )
    assert GpuChoice(1, "").label == "GPU 1 - NVIDIA GPU"


def test_gpu_choices_from_nvml_identities_skips_bad_rows_and_dupes() -> None:
    choices = gpu_choices_from_nvml_identities(
        [
            SimpleNamespace(
                index=0,
                name="RTX 4090",
                pci_bus_id="00000000:01:00.0",
                uuid="GPU-abc",
            ),
            SimpleNamespace(index="x", name="bad-index"),
            SimpleNamespace(index=0, name="duplicate"),
            SimpleNamespace(index=1, name="RTX 4080"),
        ]
    )
    assert [(c.index, c.name) for c in choices] == [(0, "RTX 4090"), (1, "RTX 4080")]
    assert choices[1].pci_bus_id == ""


def test_detected_gpu_choices_empty_without_nvml_identities(monkeypatch) -> None:
    monkeypatch.setattr(gpu_selection, "query_nvml_gpu_identities", lambda: [])
    assert gpu_selection.detected_gpu_choices() == []


def test_detected_gpu_choices_reads_nvml_identities(monkeypatch) -> None:
    monkeypatch.setattr(
        gpu_selection,
        "query_nvml_gpu_identities",
        lambda: [
            SimpleNamespace(
                index=2,
                name="RTX 5090",
                pci_bus_id="00000000:03:00.0",
                uuid="GPU-test",
            )
        ],
    )
    assert gpu_selection.detected_gpu_choices() == [
        GpuChoice(2, "RTX 5090", "00000000:03:00.0", "GPU-test")
    ]


def test_gpu_choices_with_fallback_synthesizes_when_none_detected() -> None:
    choices, selected = gpu_choices_with_fallback(selected_index=2, choices=[])
    assert selected == 2
    assert choices == [GpuChoice(index=2, name="NVIDIA GPU")]


def test_gpu_choices_with_fallback_appends_missing_selected() -> None:
    detected = [GpuChoice(0, "A"), GpuChoice(1, "B")]
    choices, selected = gpu_choices_with_fallback(selected_index=5, choices=detected)
    assert selected == 5
    assert [c.index for c in choices] == [0, 1, 5]


def test_runtime_gpu_index_reads_config_and_defaults(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        gpu_selection, "load_raw_runtime_config", lambda _p: {"gpu": {"index": 3}}
    )
    assert runtime_gpu_index(tmp_path / "c.toml") == 3

    def _boom(_p):
        raise RuntimeError("bad config")

    monkeypatch.setattr(gpu_selection, "load_raw_runtime_config", _boom)
    assert runtime_gpu_index(tmp_path / "c.toml") == 0


def test_persist_runtime_gpu_index_writes_selected(tmp_path, monkeypatch) -> None:
    written = {}
    monkeypatch.setattr(gpu_selection, "load_raw_runtime_config", lambda _p: {"other": 1})
    monkeypatch.setattr(
        gpu_selection, "write_config", lambda path, cfg: written.update(cfg)
    )
    result = persist_runtime_gpu_index(4, config_path=tmp_path / "c.toml")
    assert result == 4
    assert written["gpu"]["index"] == 4
    assert written["other"] == 1


def test_persist_runtime_gpu_index_handles_load_failure(tmp_path, monkeypatch) -> None:
    written = {}

    def _boom(_p):
        raise RuntimeError("bad config")

    monkeypatch.setattr(gpu_selection, "load_raw_runtime_config", _boom)
    monkeypatch.setattr(
        gpu_selection, "write_config", lambda path, cfg: written.update(cfg)
    )
    assert persist_runtime_gpu_index(-1, config_path=tmp_path / "c.toml") == 0
    assert written["gpu"]["index"] == 0


# --- ui/tuning.py -------------------------------------------------------------


def test_performance_preset_label_and_tooltip() -> None:
    assert auto_uv_performance_preset_label() == "Performance"
    assert "6 V/F bins" in auto_uv_performance_preset_tooltip()


def test_performance_target_default_for_unknown_gpu() -> None:
    target = auto_uv_performance_target_default(gpu_name="totally-unknown-gpu-9999")
    assert target.preset_matched is False
    assert target.voltage_mv is None
    assert target.gpu_name == "totally-unknown-gpu-9999"


def test_auto_uv_nvml_info_text_is_compact_and_tuning_relevant() -> None:
    text = auto_uv_nvml_info_text(
        AutoUvNvmlInfo(
            power_draw_w=42.25,
            power_management_enabled=True,
            power_limit_w=320.0,
            power_limit_default_w=350.0,
            power_limit_min_w=200.0,
            power_limit_max_w=450.0,
            graphics_clock_mhz=2100,
            memory_clock_mhz=10501,
            supported_memory_clocks_mhz=(810, 5001, 10501),
            supported_graphics_clock_steps_mhz=(210, 3000, 3015),
        )
    )

    assert "Power limit: current 320 W | default 350 W | range 200-450 W" in text
    assert "Current draw" not in text
    assert "42.2 W" not in text
    assert "Clocks now: core 2100 MHz | memory 10501 MHz" in text
    assert "Supported memory clocks: 810, 5001, 10501 MHz" in text
    assert "Supported core range: 210-3015 MHz (3 steps)" in text
    assert "Power management: enabled" in text
    assert "RTX" not in text
    assert "PCI" not in text
    assert "thermal" not in text.lower()
    assert "perf cap" not in text.lower()


def test_auto_uv_nvml_info_text_summarizes_long_clock_lists() -> None:
    text = auto_uv_nvml_info_text(
        AutoUvNvmlInfo(
            supported_memory_clocks_mhz=(1000, 2000, 3000, 4000, 5000, 6000),
        )
    )

    assert text == "Supported memory clocks: 1000-6000 MHz (6 steps)"


class _FakeController:
    def __init__(self, *, gpu_index, raise_on_query=False, driver_range=(0, 1500)):
        self._raise = raise_on_query
        self._range = driver_range
        self.closed = False

    def get_memory_clock_offset_range_mhz(self):
        if self._raise:
            raise RuntimeError("nvml down")
        return self._range

    def close(self):
        self.closed = True


def test_memory_offset_range_clamps_driver_max(monkeypatch) -> None:
    monkeypatch.setattr(
        "drivers.nvidia.nvml_gpu_policy.NvmlGpuPolicyController",
        lambda *, gpu_index: _FakeController(gpu_index=gpu_index, driver_range=(0, 1500)),
    )
    assert memory_offset_mhz_range() == (0, 1500)


def test_memory_offset_range_falls_back_on_error(monkeypatch) -> None:
    monkeypatch.setattr(
        "drivers.nvidia.nvml_gpu_policy.NvmlGpuPolicyController",
        lambda *, gpu_index: _FakeController(gpu_index=gpu_index, raise_on_query=True),
    )
    assert memory_offset_mhz_range() == (0, 2000)


def test_memory_offset_range_falls_back_on_empty_range(monkeypatch) -> None:
    monkeypatch.setattr(
        "drivers.nvidia.nvml_gpu_policy.NvmlGpuPolicyController",
        lambda *, gpu_index: _FakeController(gpu_index=gpu_index, driver_range=()),
    )
    assert memory_offset_mhz_range() == (0, 2000)


def test_tuning_uses_shared_runtime_gpu_index() -> None:
    # tuning now delegates to the single shared gpu_selection.runtime_gpu_index
    # (behaviour itself is covered by test_runtime_gpu_index_reads_config_and_defaults).
    assert tuning.runtime_gpu_index is gpu_selection.runtime_gpu_index
