"""Unit tests for the pure Afterburner -> Linux GPU-policy translation helpers."""
from __future__ import annotations

from afterburner.policy import (
    MAX_AFTERBURNER_MEM_OFFSET_MHZ,
    _resolve_power_limit_cap,
    afterburner_offset_khz_to_mhz,
    apply_translated_gpu_policy,
    clamp_afterburner_mem_offset_mhz,
    describe_translated_gpu_policy,
    translate_afterburner_gpu_policy,
    translate_afterburner_power_limit_pct,
)


def test_afterburner_offset_khz_to_mhz_rounds_to_nearest_mhz():
    assert afterburner_offset_khz_to_mhz(None) is None
    assert afterburner_offset_khz_to_mhz(1000) == 1
    assert afterburner_offset_khz_to_mhz(2400) == 2
    assert afterburner_offset_khz_to_mhz(2600) == 3
    assert afterburner_offset_khz_to_mhz(-1000) == -1


def test_clamp_afterburner_mem_offset_clamps_to_limit():
    assert clamp_afterburner_mem_offset_mhz(None) is None
    assert clamp_afterburner_mem_offset_mhz(500) == 500
    assert clamp_afterburner_mem_offset_mhz(MAX_AFTERBURNER_MEM_OFFSET_MHZ + 500) == (
        MAX_AFTERBURNER_MEM_OFFSET_MHZ
    )
    assert clamp_afterburner_mem_offset_mhz(-(MAX_AFTERBURNER_MEM_OFFSET_MHZ + 500)) == (
        -MAX_AFTERBURNER_MEM_OFFSET_MHZ
    )


def test_resolve_power_limit_cap_only_honours_positive_manual_caps():
    assert _resolve_power_limit_cap(None, {}) == (None, "none")
    assert _resolve_power_limit_cap(200, {}) == (200, "manual")
    assert _resolve_power_limit_cap(0, {}) == (None, "none")
    assert _resolve_power_limit_cap(-5, {}) == (None, "none")


def test_translate_power_limit_pct_none_returns_empty_target():
    result = translate_afterburner_power_limit_pct(None, power_limits={})
    assert result == {
        "power_limit_source_w": None,
        "power_limit_w": None,
        "power_limit_cap_w": None,
        "power_limit_cap_mode": "none",
    }


def test_translate_power_limit_pct_none_still_surfaces_manual_cap():
    result = translate_afterburner_power_limit_pct(
        None, power_limits={}, power_limit_cap_w=200
    )
    assert result["power_limit_cap_w"] == 200
    assert result["power_limit_cap_mode"] == "manual"
    assert result["power_limit_w"] is None


def test_translate_power_limit_pct_without_default_yields_no_target():
    result = translate_afterburner_power_limit_pct(90, power_limits={})
    assert result["power_limit_source_w"] is None
    assert result["power_limit_w"] is None


def test_translate_power_limit_pct_scales_from_default():
    result = translate_afterburner_power_limit_pct(
        90,
        power_limits={
            "power_limit_default_w": 250,
            "power_limit_min_w": 100,
            "power_limit_max_w": 300,
        },
    )
    assert result["power_limit_source_w"] == 225
    assert result["power_limit_w"] == 225
    assert result["power_limit_cap_w"] is None


def test_translate_power_limit_pct_respects_min_and_max_bounds():
    low = translate_afterburner_power_limit_pct(
        50, power_limits={"power_limit_default_w": 100, "power_limit_min_w": 80}
    )
    assert low["power_limit_w"] == 80

    high = translate_afterburner_power_limit_pct(
        150, power_limits={"power_limit_default_w": 300, "power_limit_max_w": 320}
    )
    assert high["power_limit_w"] == 320


def test_translate_power_limit_pct_applies_manual_cap():
    result = translate_afterburner_power_limit_pct(
        90,
        power_limits={
            "power_limit_default_w": 250,
            "power_limit_min_w": 100,
            "power_limit_max_w": 300,
        },
        power_limit_cap_w=200,
    )
    assert result["power_limit_source_w"] == 225
    assert result["power_limit_w"] == 200
    assert result["power_limit_cap_w"] == 200
    assert result["power_limit_cap_mode"] == "manual"


def test_translate_gpu_policy_derives_clock_and_power_fields():
    policy = translate_afterburner_gpu_policy(
        {
            "power_limit_pct": 90,
            "core_clk_boost_khz": 150000,
            "mem_clk_boost_khz": 1000000,
            "thermal_limit_raw": "83",
        },
        power_limits={
            "power_limit_default_w": 250,
            "power_limit_min_w": 100,
            "power_limit_max_w": 300,
        },
    )
    assert policy["power_limit_w"] == 225
    assert policy["power_limit_source_w"] == 225
    assert policy["mem_clk_vf_offset_requested_mhz"] == 1000
    assert policy["mem_clk_vf_offset_mhz"] == 1000
    assert policy["mem_clk_vf_offset_limit_mhz"] == MAX_AFTERBURNER_MEM_OFFSET_MHZ
    assert policy["core_clk_boost_linux_mode"] == "unmapped"
    assert policy["thermal_limit_raw"] == "83"


def test_translate_gpu_policy_clamps_excessive_mem_offset():
    policy = translate_afterburner_gpu_policy(
        {"mem_clk_boost_khz": 3_000_000},
        power_limits={},
    )
    assert policy["mem_clk_vf_offset_requested_mhz"] == 3000
    assert policy["mem_clk_vf_offset_mhz"] == MAX_AFTERBURNER_MEM_OFFSET_MHZ


def test_translate_gpu_policy_marks_absent_core_boost_as_none():
    policy = translate_afterburner_gpu_policy({}, power_limits={})
    assert policy["core_clk_boost_linux_mode"] == "none"
    assert policy["mem_clk_vf_offset_mhz"] is None


def test_describe_translated_gpu_policy_empty_is_none():
    assert describe_translated_gpu_policy({}) == "none"
    assert describe_translated_gpu_policy(None) == "none"


def test_describe_translated_gpu_policy_capped_power():
    text = describe_translated_gpu_policy(
        {
            "power_limit_pct": 90,
            "power_limit_source_w": 225,
            "power_limit_w": 200,
            "power_limit_cap_w": 200,
        }
    )
    assert text == "power-limit=90%->225W capped(manual)->200W"


def test_describe_translated_gpu_policy_uncapped_power_variants():
    same = describe_translated_gpu_policy(
        {"power_limit_pct": 90, "power_limit_source_w": 225, "power_limit_w": 225}
    )
    assert same == "power-limit=90%->225W"

    pct_only = describe_translated_gpu_policy({"power_limit_pct": 90})
    assert pct_only == "power-limit=90%"

    w_only = describe_translated_gpu_policy({"power_limit_w": 200})
    assert w_only == "power-limit=200W"

    # source differs from final but no manual cap: report the final wattage only.
    clamped_no_cap = describe_translated_gpu_policy(
        {
            "power_limit_pct": 90,
            "power_limit_source_w": 225,
            "power_limit_w": 200,
            "power_limit_cap_w": None,
        }
    )
    assert clamped_no_cap == "power-limit=90%->200W"


def test_describe_translated_gpu_policy_core_boost_unmapped():
    text = describe_translated_gpu_policy(
        {"core_clk_boost_khz": 150000, "core_clk_boost_linux_mode": "unmapped"}
    )
    assert text == "core-boost=150000kHz->unmapped"


def test_describe_translated_gpu_policy_mem_offset_variants():
    clamped = describe_translated_gpu_policy(
        {
            "mem_clk_boost_khz": 3_000_000,
            "mem_clk_vf_offset_requested_mhz": 3000,
            "mem_clk_vf_offset_mhz": 2000,
            "mem_clk_vf_offset_limit_mhz": 2000,
        }
    )
    assert clamped == (
        "mem-boost=3000000kHz->+3000MHz clamped->+2000MHz (limit +2000MHz)"
    )

    normal = describe_translated_gpu_policy(
        {
            "mem_clk_boost_khz": 1_000_000,
            "mem_clk_vf_offset_requested_mhz": 1000,
            "mem_clk_vf_offset_mhz": 1000,
        }
    )
    assert normal == "mem-boost=1000000kHz->+1000MHz"

    offset_only = describe_translated_gpu_policy({"mem_clk_vf_offset_mhz": 500})
    assert offset_only == "mem-vf-offset=+500MHz"


def test_describe_translated_gpu_policy_thermal_limit():
    text = describe_translated_gpu_policy({"thermal_limit_raw": "83"})
    assert text == "thermal-limit=unsupported(83)"


class _RecordingController:
    def __init__(self):
        self.power_limit_w = None
        self.offsets_called_with = None

    def apply_power_limit_w(self, value):
        self.power_limit_w = value

    def apply_clock_offsets(self, *, mem_clk_vf_offset_mhz):
        self.offsets_called_with = mem_clk_vf_offset_mhz
        return {"mem_clk_vf_offset_mhz": int(mem_clk_vf_offset_mhz)}


def test_apply_translated_gpu_policy_applies_power_and_offsets():
    controller = _RecordingController()
    applied = apply_translated_gpu_policy(
        controller, {"power_limit_w": 200, "mem_clk_vf_offset_mhz": 100}
    )
    assert controller.power_limit_w == 200
    assert controller.offsets_called_with == 100
    assert applied == {"power_limit_w": 200, "mem_clk_vf_offset_mhz": 100}


def test_apply_translated_gpu_policy_noop_when_nothing_to_apply():
    controller = _RecordingController()
    applied = apply_translated_gpu_policy(
        controller, {"power_limit_w": None, "mem_clk_vf_offset_mhz": None}
    )
    assert controller.power_limit_w is None
    assert controller.offsets_called_with is None
    assert applied == {}
