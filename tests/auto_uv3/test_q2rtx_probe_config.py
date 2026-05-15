from __future__ import annotations

from stability.q2rtx import Q2RTXStabilityConfig

from auto_uv3.q2rtx.q2rtx_cuda_probe_config import short_q2rtx_probe_config
from auto_uv3.scan_runtime_settings import read_scan_runtime_settings


def test_short_probe_config_uses_duration_for_runtime_loop_calibration() -> None:
    config = short_q2rtx_probe_config(
        Q2RTXStabilityConfig(timedemo_loops=99, single_pass_timeout_s=999.0),
        target_duration_s=20,
    )

    assert config.timedemo_loops is None
    assert config.duration_s == 20
    assert config.single_pass_timeout_s == 120.0


def test_scan_runtime_settings_do_not_precompute_timedemo_loops() -> None:
    source_config = Q2RTXStabilityConfig(duration_s=600, single_pass_timeout_s=999.0)

    settings = read_scan_runtime_settings({}, source_config)

    assert settings.q2rtx_config is source_config
    assert settings.q2rtx_config.timedemo_loops is None
    assert settings.q2rtx_config.duration_s == 600
    assert settings.q2rtx_config.single_pass_timeout_s == 999.0


def test_scan_runtime_tail_rise_defaults_follow_auto_uv_mode() -> None:
    source_config = Q2RTXStabilityConfig(duration_s=600)

    efficiency = read_scan_runtime_settings({"auto_uv_mode": "efficiency"}, source_config)
    performance = read_scan_runtime_settings({"auto_uv_mode": "performance"}, source_config)
    overridden = read_scan_runtime_settings(
        {"auto_uv_mode": "performance", "auto_uv_tail_rise_bins": 4},
        source_config,
    )

    assert efficiency.tail_rise_bins == 0
    assert performance.tail_rise_bins == 4
    assert overridden.tail_rise_bins == 4
