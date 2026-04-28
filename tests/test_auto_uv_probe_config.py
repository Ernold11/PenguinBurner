from __future__ import annotations

import pytest

from auto_uv.probe_config import (
    _budget_final_probe_durations,
    _budget_tiered_probe_durations,
    _stability_probe_config_for_voltage_band,
    build_long_stability_test_config,
    long_stability_workload_durations,
)
from auto_uv.probe_runner import (
    _expected_timedemo_loop_hint_s,
    _progress_elapsed_s_for_ui,
    _progress_target_duration_s,
)
from stability.q2rtx import Q2RTXStabilityConfig


def _companion_duration_s(config: Q2RTXStabilityConfig) -> int:
    command = list(config.companion_command or ())
    index = command.index("--duration-seconds")
    return int(command[index + 1])


def test_tiered_probe_budget_allows_low_voltage_90s_tier() -> None:
    assert _budget_tiered_probe_durations(30) == (15, 15)
    assert _budget_tiered_probe_durations(60) == (45, 15)
    assert _budget_tiered_probe_durations(90) == (75, 15)
    assert _budget_tiered_probe_durations(120) == (105, 15)


def test_voltage_band_probe_config_uses_30_60_90_q2rtx_durations() -> None:
    config = Q2RTXStabilityConfig(duration_s=0, demo_name="unknown-demo", gpu_index=0)

    high = _stability_probe_config_for_voltage_band(
        config,
        initial_target_voltage_mv=1000,
        candidate_voltage_mv=960,
    )
    medium = _stability_probe_config_for_voltage_band(
        config,
        initial_target_voltage_mv=1000,
        candidate_voltage_mv=925,
    )
    low = _stability_probe_config_for_voltage_band(
        config,
        initial_target_voltage_mv=1000,
        candidate_voltage_mv=875,
    )

    assert (high.duration_s, _companion_duration_s(high)) == (30, 15)
    assert (medium.duration_s, _companion_duration_s(medium)) == (60, 15)
    assert (low.duration_s, _companion_duration_s(low)) == (90, 15)


def test_voltage_band_probe_config_scales_from_base_duration() -> None:
    config = Q2RTXStabilityConfig(duration_s=0, demo_name="unknown-demo", gpu_index=0)

    high = _stability_probe_config_for_voltage_band(
        config,
        initial_target_voltage_mv=1000,
        candidate_voltage_mv=960,
        base_duration_s=40,
    )
    medium = _stability_probe_config_for_voltage_band(
        config,
        initial_target_voltage_mv=1000,
        candidate_voltage_mv=925,
        base_duration_s=40,
    )
    low = _stability_probe_config_for_voltage_band(
        config,
        initial_target_voltage_mv=1000,
        candidate_voltage_mv=875,
        base_duration_s=40,
    )

    assert (high.duration_s, _companion_duration_s(high)) == (40, 20)
    assert (medium.duration_s, _companion_duration_s(medium)) == (80, 20)
    assert (low.duration_s, _companion_duration_s(low)) == (120, 20)


def test_voltage_band_probe_config_clamps_short_base_to_10s_and_full_loops() -> None:
    config = Q2RTXStabilityConfig(duration_s=0, demo_name="q2demo1", gpu_index=0)

    high = _stability_probe_config_for_voltage_band(
        config,
        initial_target_voltage_mv=1000,
        candidate_voltage_mv=960,
        base_duration_s=5,
    )

    assert high.duration_s == 0
    assert high.timedemo_loops == 3
    assert _companion_duration_s(high) == 5


def test_final_verification_cuda_split_stays_at_existing_ratio() -> None:
    assert _budget_final_probe_durations(600) == (450, 150)
    assert long_stability_workload_durations(
        600,
        include_q2rtx=True,
        include_cuda=True,
    ) == (450, 150)


def test_long_stability_both_workloads_use_auto_uv_split() -> None:
    config = Q2RTXStabilityConfig(duration_s=0, demo_name="unknown-demo", gpu_index=0)

    configured = build_long_stability_test_config(
        config,
        total_duration_s=600,
        include_q2rtx=True,
        include_cuda=True,
    )

    assert configured.duration_s == 450
    assert _companion_duration_s(configured) == 150


def test_long_stability_q2rtx_only_uses_full_duration() -> None:
    config = Q2RTXStabilityConfig(duration_s=0, demo_name="unknown-demo", gpu_index=0)

    configured = build_long_stability_test_config(
        config,
        total_duration_s=600,
        include_q2rtx=True,
        include_cuda=False,
    )

    assert configured.duration_s == 600
    assert configured.companion_command is None


def test_long_stability_cuda_only_uses_full_duration() -> None:
    config = Q2RTXStabilityConfig(duration_s=0, demo_name="unknown-demo", gpu_index=0)

    configured = build_long_stability_test_config(
        config,
        total_duration_s=600,
        include_q2rtx=False,
        include_cuda=True,
    )

    assert configured.duration_s == 600
    assert _companion_duration_s(configured) == 600


def test_long_stability_rejects_empty_workload() -> None:
    config = Q2RTXStabilityConfig(duration_s=0, demo_name="unknown-demo", gpu_index=0)

    with pytest.raises(ValueError):
        build_long_stability_test_config(
            config,
            total_duration_s=600,
            include_q2rtx=False,
            include_cuda=False,
        )


def test_final_progress_target_uses_configured_total_duration() -> None:
    config = Q2RTXStabilityConfig(duration_s=0, timedemo_loops=102)

    assert (
        _progress_target_duration_s(
            q2rtx_config=config,
            companion_duration_s=150.0,
            expected_loop_s=4.117,
            state={"expected_runs": 102},
            expected_total_duration_s=600,
        )
        == 600.0
    )


def test_default_demo_has_progress_duration_hint_before_first_loop() -> None:
    config = Q2RTXStabilityConfig(duration_s=0, demo_name="auto", timedemo_loops=3)

    hint = _expected_timedemo_loop_hint_s(config)

    assert hint is not None
    assert (
        _progress_target_duration_s(
            q2rtx_config=config,
            companion_duration_s=5.0,
            expected_loop_s=hint,
            state={},
        )
        > 5.0
    )


def test_ui_progress_prefers_smooth_wall_elapsed_over_completed_loop_elapsed() -> None:
    assert (
        _progress_elapsed_s_for_ui(
            {
                "progress_elapsed_s": 12.0,
                "net_elapsed_s": 0.0,
                "elapsed_s": 12.0,
            }
        )
        == 12.0
    )
    assert _progress_elapsed_s_for_ui({"net_elapsed_s": 17.0}) == 17.0
