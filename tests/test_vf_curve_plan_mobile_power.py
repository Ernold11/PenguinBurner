from __future__ import annotations

import json
from types import SimpleNamespace

from runtime.support import vf_curve_plan


class _FakeProg:
    freq_offset_khz = 0


class _FakeVfPoint:
    def __init__(self) -> None:
        self.prog = _FakeProg()


class _FakeControl:
    def __init__(self) -> None:
        self.vf_points = [_FakeVfPoint()]


class _FakeReader:
    def __init__(self) -> None:
        self.control = _FakeControl()
        self.applied = None

    def editable_core_points(self):
        return [
            {
                "index": 0,
                "voltage_uv": 900_000,
                "base_freq_khz": 2_400_000,
                "current_offset_khz": 12_000,
            }
        ]

    def get_control_struct(self):
        return self.control

    def set_control_struct(self, control) -> None:
        self.applied = control


def _capabilities():
    return SimpleNamespace(
        identity=SimpleNamespace(
            name="NVIDIA GeForce RTX 2050",
            pci_device_id="0x25B810DE",
        ),
        power=SimpleNamespace(
            current_w=43.0,
            default_w=61.0,
            minimum_w=35.0,
            maximum_w=80.0,
        ),
        clock_offsets=SimpleNamespace(memory_mhz=500),
    )


class _PolicyController:
    def __init__(self, *, power_limit_supported: bool = False) -> None:
        self.power_limit_supported = bool(power_limit_supported)
        self.clock_offset_calls = []

    def capabilities(self):
        return _capabilities()

    def power_limit_set_supported(self) -> bool:
        return self.power_limit_supported

    def apply_power_limit_w(self, _watts):
        raise AssertionError("unsupported fixed power-limit setter must not run")

    def apply_clock_offsets(self, **kwargs):
        self.clock_offset_calls.append(kwargs)
        return dict(kwargs)


def test_backup_current_offsets_skips_driver_unsupported_power_limit(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        vf_curve_plan,
        "claim_desktop_user_ownership",
        lambda *_a, **_k: None,
    )

    path = vf_curve_plan.backup_current_offsets(
        _FakeReader(),
        tmp_path / "backup.json",
        policy_controller=_PolicyController(),
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    # No settable power limit is stored, so restore never tries to apply one.
    assert "power_limit_w" not in payload["gpu_policy"]
    assert "power_limit_default_w" not in payload["gpu_policy"]
    assert payload["gpu_policy"]["mem_clk_vf_offset_mhz"] == 500


def test_backup_current_offsets_keeps_driver_supported_power_limit(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        vf_curve_plan,
        "claim_desktop_user_ownership",
        lambda *_a, **_k: None,
    )

    path = vf_curve_plan.backup_current_offsets(
        _FakeReader(),
        tmp_path / "backup.json",
        policy_controller=_PolicyController(power_limit_supported=True),
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["gpu_policy"]["power_limit_w"] == 43
    assert payload["gpu_policy"]["power_limit_default_w"] == 61


def test_restore_offsets_without_power_limit_does_not_apply_power(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        vf_curve_plan,
        "claim_desktop_user_ownership",
        lambda *_a, **_k: None,
    )
    backup = tmp_path / "backup.json"
    # A backup created after an unsupported probe omits power, so restore only
    # replays the V/F offsets.
    backup.write_text(
        json.dumps(
            {
                "points": [{"index": 0, "current_offset_mhz": 12}],
                "gpu_policy": {"mem_clk_vf_offset_mhz": 500},
            }
        ),
        encoding="utf-8",
    )
    controller = _PolicyController()

    restored = vf_curve_plan.restore_offsets(
        _FakeReader(),
        backup,
        policy_controller=controller,
    )

    assert restored == 1
    assert controller.clock_offset_calls == [{"mem_clk_vf_offset_mhz": 500}]
