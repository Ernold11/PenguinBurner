"""Tests for the adaptive-mode wiring helpers."""

from __future__ import annotations

from auto_uv.adaptive_main import (
    SECONDARY_FINAL_VERIFICATION_S,
    adaptive_clock_ceiling_mhz,
    final_verification_duration_for_tier,
)
from auto_uv.scan_mode.auto_uv_mode import (
    AUTO_UV_MODE_ADAPTIVE,
    normalize_auto_uv_mode,
)


def test_adaptive_mode_normalizes_and_is_not_swallowed() -> None:
    assert normalize_auto_uv_mode("adaptive") == AUTO_UV_MODE_ADAPTIVE
    assert normalize_auto_uv_mode("all") == AUTO_UV_MODE_ADAPTIVE
    assert normalize_auto_uv_mode("ADAPTIVE ") == AUTO_UV_MODE_ADAPTIVE
    # The classic aliases keep their meaning.
    assert normalize_auto_uv_mode("") == "efficiency"
    assert normalize_auto_uv_mode("aggressive") == "performance"


def test_adaptive_clock_ceiling_is_the_perf_table_point() -> None:
    # The 5080 do-not-exceed rule: never request above 2980MHz.
    assert adaptive_clock_ceiling_mhz("NVIDIA GeForce RTX 5080") == 2980
    assert adaptive_clock_ceiling_mhz("Totally Unknown GPU 9000") is None


def test_graduated_final_durations() -> None:
    # The most fragile (deepest) profile soaks the full configured duration;
    # the rest get the shorter confirm, never longer than configured.
    assert final_verification_duration_for_tier(0, configured_final_duration_s=300) == 300
    assert (
        final_verification_duration_for_tier(1, configured_final_duration_s=300)
        == SECONDARY_FINAL_VERIFICATION_S
    )
    assert final_verification_duration_for_tier(2, configured_final_duration_s=60) == 60
