from __future__ import annotations

from stability.q2rtx import Q2RTXStabilityConfig

from auto_uv.q2rtx.q2rtx_cuda_probe_config import cuda_companion_enabled_for_voltage_band
from auto_uv.q2rtx.q2rtx_cuda_probe_config import q2rtx_cuda_probe_config_for_voltage_band
from auto_uv.q2rtx.q2rtx_cuda_probe_config import q2rtx_only_probe_config_for_voltage_band
from auto_uv.q2rtx.q2rtx_cuda_probe_config import reference_discovery_q2rtx_probe_config
from auto_uv.q2rtx.q2rtx_cuda_probe_config import short_q2rtx_probe_config
from auto_uv.q2rtx.q2rtx_cuda_probe_config import tiered_cuda_probe_duration_s
from auto_uv.q2rtx.q2rtx_cuda_probe_config import tiered_q2rtx_probe_duration_s
from auto_uv.scan_runtime_settings import read_scan_runtime_settings


def test_short_probe_config_uses_duration_for_runtime_loop_calibration() -> None:
    config = short_q2rtx_probe_config(
        Q2RTXStabilityConfig(timedemo_loops=99, single_pass_timeout_s=999.0),
        target_duration_s=20,
    )

    assert config.timedemo_loops is None
    assert config.duration_s == 20
    assert config.single_pass_timeout_s == 120.0


def test_reference_discovery_probe_runs_double_q2rtx_without_cuda() -> None:
    config = reference_discovery_q2rtx_probe_config(
        Q2RTXStabilityConfig(companion_command=("cuda",)),
        base_duration_s=10,
    )

    assert config.duration_s == 20
    assert config.timedemo_loops is None
    assert config.companion_command is None


def test_q2rtx_only_voltage_band_probe_clears_cuda() -> None:
    config = q2rtx_only_probe_config_for_voltage_band(
        Q2RTXStabilityConfig(companion_command=("cuda",)),
        initial_target_voltage_mv=1000,
        candidate_voltage_mv=900,
        base_duration_s=10,
    )

    assert config.duration_s == 20
    assert config.companion_command is None


def test_voltage_band_probe_skips_cuda_inside_first_five_percent_drop() -> None:
    q2rtx_s = tiered_q2rtx_probe_duration_s(
        initial_target_voltage_mv=1000,
        candidate_voltage_mv=980,
        base_duration_s=10,
    )
    cuda_s = tiered_cuda_probe_duration_s(base_duration_s=10)

    assert q2rtx_s == 10
    assert cuda_s == 5
    assert cuda_s < q2rtx_s
    assert (
        cuda_companion_enabled_for_voltage_band(
            initial_target_voltage_mv=1000,
            candidate_voltage_mv=950,
        )
        is False
    )

    config = q2rtx_cuda_probe_config_for_voltage_band(
        Q2RTXStabilityConfig(gpu_index=2),
        initial_target_voltage_mv=1000,
        candidate_voltage_mv=980,
        base_duration_s=10,
    )

    assert config.duration_s == 10
    assert config.companion_command is None


def test_voltage_band_probe_adds_cuda_after_five_percent_drop() -> None:
    config = q2rtx_cuda_probe_config_for_voltage_band(
        Q2RTXStabilityConfig(gpu_index=2),
        initial_target_voltage_mv=1000,
        candidate_voltage_mv=940,
        base_duration_s=10,
    )

    assert config.duration_s == 20
    assert _companion_duration_s(config.companion_command) == 5


def test_deeper_voltage_probe_extends_q2rtx_not_cuda() -> None:
    cuda_s = tiered_cuda_probe_duration_s(base_duration_s=10)

    assert (
        tiered_q2rtx_probe_duration_s(
            initial_target_voltage_mv=1000,
            candidate_voltage_mv=920,
            base_duration_s=10,
        ),
        cuda_s,
    ) == (20, 5)
    assert (
        tiered_q2rtx_probe_duration_s(
            initial_target_voltage_mv=1000,
            candidate_voltage_mv=850,
            base_duration_s=10,
        ),
        cuda_s,
    ) == (30, 5)


def test_scan_runtime_settings_do_not_precompute_timedemo_loops() -> None:
    source_config = Q2RTXStabilityConfig(duration_s=600, single_pass_timeout_s=999.0)

    settings = read_scan_runtime_settings(
        {},
        source_config,
        gpu_name="NVIDIA GeForce RTX 5080",
    )

    assert settings.q2rtx_config is source_config
    assert settings.q2rtx_config.timedemo_loops is None
    assert settings.q2rtx_config.duration_s == 600
    assert settings.q2rtx_config.single_pass_timeout_s == 999.0
    assert round(settings.final_clock_drop_margin_pct, 4) == 11.1111
    assert round(settings.min_performance_core_clock_pct, 4) == 88.8889
    assert settings.derive_efficiency_stop_streak is True


def test_scan_runtime_settings_use_generic_clock_drop_for_unknown_gpu() -> None:
    source_config = Q2RTXStabilityConfig(duration_s=600, single_pass_timeout_s=999.0)

    settings = read_scan_runtime_settings({}, source_config)

    assert settings.final_clock_drop_margin_pct == 12.5
    assert settings.min_performance_core_clock_pct == 87.5


def test_scan_runtime_settings_preserve_non_default_efficiency_stop_streak() -> None:
    source_config = Q2RTXStabilityConfig(duration_s=600)

    settings = read_scan_runtime_settings(
        {"auto_uv_efficiency_stop_streak": 4},
        source_config,
    )

    assert settings.efficiency_stop_streak == 4
    assert settings.derive_efficiency_stop_streak is False


def test_scan_runtime_tail_rise_defaults_follow_auto_uv_mode() -> None:
    source_config = Q2RTXStabilityConfig(duration_s=600)

    efficiency = read_scan_runtime_settings({"auto_uv_mode": "efficiency"}, source_config)
    performance = read_scan_runtime_settings({"auto_uv_mode": "performance"}, source_config)
    overridden = read_scan_runtime_settings(
        {"auto_uv_mode": "performance", "auto_uv_tail_rise_bins": 4},
        source_config,
    )

    assert efficiency.tail_rise_bins == 0
    assert performance.tail_rise_bins == 6
    assert overridden.tail_rise_bins == 4


def _companion_duration_s(command: tuple[str, ...] | None) -> int | None:
    if command is None:
        return None
    parts = [str(part) for part in command]
    for index, part in enumerate(parts[:-1]):
        if part == "--duration-seconds":
            return int(parts[index + 1])
    return None
