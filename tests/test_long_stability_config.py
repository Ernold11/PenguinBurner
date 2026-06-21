from stability.q2rtx import Q2RTXStabilityConfig
from stability.q2rtx.long_stability_config import (
    build_long_stability_test_config,
    long_stability_workload_durations,
)


def test_long_stability_duration_split_keeps_cuda_inside_total_budget() -> None:
    assert long_stability_workload_durations(600) == (450, 150)


def test_long_stability_config_adds_cuda_companion() -> None:
    config = build_long_stability_test_config(
        Q2RTXStabilityConfig(gpu_index=1, single_pass_timeout_s=9999.0),
        total_duration_s=600,
    )

    assert config.companion_command is not None
    assert "--gpu-index" in config.companion_command
    assert "1" in config.companion_command
    assert "--duration-seconds" in config.companion_command
    assert "150" in config.companion_command
    assert config.duration_s == 450
    assert config.single_pass_timeout_s == 660.0
