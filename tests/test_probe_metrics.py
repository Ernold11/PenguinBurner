from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_uv.probe_metrics import _summarize_probe


def test_summarize_probe_can_ignore_timedemo_warmup_runs() -> None:
    result = SimpleNamespace(
        timedemo_runs=[
            SimpleNamespace(fps=90.0, frames=631, seconds=7.0),
            SimpleNamespace(fps=100.0, frames=631, seconds=6.31),
            SimpleNamespace(fps=102.0, frames=631, seconds=6.19),
            SimpleNamespace(fps=98.0, frames=631, seconds=6.44),
        ],
        telemetry_samples=[],
        companion_telemetry_samples=[],
        reason="ok",
        log_path=Path("synthetic.log"),
    )
    result.telemetry_summary = lambda: {
        "sample_count": 0,
        "power_max": 205.0,
        "temperature_max": 64.0,
        "fan_max": 40.0,
        "voltage_avg": 900.0,
        "power_avg": 200.0,
        "core_clock_avg": 2650.0,
        "temperature_avg": 63.0,
        "fan_avg": 39.0,
    }

    summary = _summarize_probe(
        candidate_voltage_mv=900,
        lock_clock_mhz=2700,
        live_voltage_before_mv=895,
        live_voltage_after_mv=900,
        used_companion_load=False,
        power_limit_w=None,
        result=result,
        timedemo_warmup_runs=1,
    )

    assert summary.avg_fps == pytest.approx(100.0)
    assert summary.min_fps == pytest.approx(98.0)
    assert summary.max_fps == pytest.approx(102.0)
    assert summary.avg_seconds_per_run == pytest.approx((6.31 + 6.19 + 6.44) / 3.0)
    assert summary.efficiency_fps_per_w == pytest.approx(0.5)
