from __future__ import annotations

from auto_uv.auto_uv_types import TelemetrySample, TimedemoRun
from auto_uv.auto_uv_user_options import (
    AUTO_UV_METRIC_TUNING,
    AUTO_UV_STALL_TUNING,
)
from auto_uv.q2rtx.probe_runtime_guardrails import (
    probe_failure_should_mark_voltage_unsafe,
)
from auto_uv.q2rtx.q2rtx_live_abort_rules import (
    low_live_clock_abort_reason,
    selected_nvidia_gpu_idle_abort_reason,
    telemetry_live_abort_reason,
    timedemo_live_abort_reason,
    timedemo_stall_abort_reason,
)


def _sample(
    elapsed_s: float,
    *,
    gpu_util_pct: float,
    power_w: float,
    core_clock_mhz: float = 210.0,
) -> TelemetrySample:
    return TelemetrySample(
        elapsed_s=float(elapsed_s),
        gpu_util_pct=float(gpu_util_pct),
        power_w=float(power_w),
        core_clock_mhz=float(core_clock_mhz),
    )


def test_telemetry_abort_detects_q2rtx_not_loading_selected_nvidia_gpu() -> None:
    samples = [
        _sample(float(index), gpu_util_pct=0.0, power_w=4.5)
        for index in range(5, 24)
    ]

    reason = telemetry_live_abort_reason(
        {
            "elapsed_s": 23.6,
            "latest_sample": samples[-1],
            "telemetry_samples": samples,
        },
        busy_power_floor_w=None,
        proper_run_power_floor_w=None,
        target_core_clock_floor_mhz=None,
        progress_state={},
    )

    assert reason is not None
    assert reason.startswith("q2rtx-selected-nvidia-gpu-idle")
    assert "may be rendering on another GPU" in reason
    assert "--gpu-index" in reason


def test_telemetry_idle_abort_allows_actual_selected_gpu_load() -> None:
    samples = [
        _sample(float(index), gpu_util_pct=0.0, power_w=4.5)
        for index in range(5, 35)
    ]
    samples.append(
        _sample(35.0, gpu_util_pct=97.0, power_w=95.0, core_clock_mhz=2400.0)
    )

    assert (
        telemetry_live_abort_reason(
            {
                "elapsed_s": 35.0,
                "latest_sample": samples[-1],
                "telemetry_samples": samples,
            },
            busy_power_floor_w=None,
            proper_run_power_floor_w=None,
            target_core_clock_floor_mhz=None,
            progress_state={},
        )
        is None
    )


def test_selected_gpu_idle_failure_does_not_blacklist_voltage() -> None:
    assert (
        probe_failure_should_mark_voltage_unsafe(
            "q2rtx-selected-nvidia-gpu-idle max_util=0.0% max_power=4.5W"
        )
        is False
    )


# --------------------------------------------------------------------------
# timedemo_live_abort_reason
# --------------------------------------------------------------------------


def _run(
    *,
    frames: int = 600,
    seconds: float = 10.0,
    fps: float = 60.0,
    run_index: int = 2,
) -> TimedemoRun:
    return TimedemoRun(
        frames=int(frames),
        seconds=float(seconds),
        fps=float(fps),
        run_index=int(run_index),
    )


def test_timedemo_abort_flags_invalid_metrics() -> None:
    for bad in (
        _run(frames=0),
        _run(seconds=0.0),
        _run(fps=0.0),
    ):
        reason = timedemo_live_abort_reason(
            {"new_timedemo_runs": [bad]},
            frame_reference=600,
            proper_run_fps_floor=50.0,
            progress_state={},
        )
        assert reason == "timedemo-metrics-invalid"


def test_timedemo_abort_flags_frame_count_mismatch() -> None:
    reason = timedemo_live_abort_reason(
        {"new_timedemo_runs": [_run(frames=599, run_index=3)]},
        frame_reference=600,
        proper_run_fps_floor=50.0,
        progress_state={},
    )

    assert reason is not None
    assert reason.startswith("timedemo-live-frame-count")
    assert "current=599" in reason
    assert "expected=600" in reason
    assert "run=3" in reason


def test_timedemo_frame_reference_falls_back_to_state_expected() -> None:
    reason = timedemo_live_abort_reason(
        {
            "new_timedemo_runs": [_run(frames=720)],
            "expected_frames_per_run": 600,
        },
        frame_reference=None,
        proper_run_fps_floor=50.0,
        progress_state={},
    )

    assert reason is not None
    assert reason.startswith("timedemo-live-frame-count")
    assert "expected=600" in reason


def test_timedemo_warmup_run_resets_low_fps_streak() -> None:
    progress_state = {"low_fps_streak": 5}

    # run_index <= 1 is a warmup run: streak resets and no abort, even on low fps.
    reason = timedemo_live_abort_reason(
        {"new_timedemo_runs": [_run(fps=1.0, run_index=1)]},
        frame_reference=600,
        proper_run_fps_floor=50.0,
        progress_state=progress_state,
    )

    assert reason is None
    assert progress_state["low_fps_streak"] == 0


def test_timedemo_no_fps_floor_resets_streak_and_passes() -> None:
    progress_state = {"low_fps_streak": 5}

    reason = timedemo_live_abort_reason(
        {"new_timedemo_runs": [_run(fps=1.0, run_index=4)]},
        frame_reference=600,
        proper_run_fps_floor=None,
        progress_state=progress_state,
    )

    assert reason is None
    assert progress_state["low_fps_streak"] == 0


def test_timedemo_healthy_fps_resets_streak_without_abort() -> None:
    progress_state = {"low_fps_streak": 1}

    reason = timedemo_live_abort_reason(
        {"new_timedemo_runs": [_run(fps=80.0, run_index=4)]},
        frame_reference=600,
        proper_run_fps_floor=50.0,
        progress_state=progress_state,
    )

    assert reason is None
    assert progress_state["low_fps_streak"] == 0


def test_timedemo_single_low_fps_run_below_streak_does_not_abort() -> None:
    progress_state: dict = {}

    reason = timedemo_live_abort_reason(
        {"new_timedemo_runs": [_run(fps=10.0, run_index=4)]},
        frame_reference=600,
        proper_run_fps_floor=50.0,
        progress_state=progress_state,
    )

    # Streak of 1 is below min_proper_run_fps_regression_streak (2): no abort yet.
    assert reason is None
    assert progress_state["low_fps_streak"] == 1


def test_timedemo_low_fps_regression_streak_aborts() -> None:
    progress_state: dict = {}
    runs = [
        _run(fps=10.0, run_index=4),
        _run(fps=10.0, run_index=5),
    ]

    reason = timedemo_live_abort_reason(
        {"new_timedemo_runs": runs},
        frame_reference=600,
        proper_run_fps_floor=50.0,
        progress_state=progress_state,
    )

    assert reason is not None
    assert reason.startswith("timedemo-live-fps-regression")
    assert "current=10.0" in reason
    assert "floor=50.0" in reason
    assert "streak=2" in reason
    assert "run=5" in reason


# --------------------------------------------------------------------------
# selected_nvidia_gpu_idle_abort_reason guards
# --------------------------------------------------------------------------


def test_selected_gpu_idle_skips_when_elapsed_below_min() -> None:
    samples = [_sample(float(i), gpu_util_pct=0.0, power_w=4.5) for i in range(5, 24)]
    state = {
        "elapsed_s": AUTO_UV_STALL_TUNING.selected_gpu_idle_min_s - 1.0,
        "telemetry_samples": samples,
    }

    assert selected_nvidia_gpu_idle_abort_reason(state) is None


def test_selected_gpu_idle_skips_with_too_few_warmed_samples() -> None:
    # Only a couple of post-warmup samples -> below selected_gpu_idle_min_samples.
    samples = [_sample(float(i), gpu_util_pct=0.0, power_w=4.5) for i in range(6, 9)]
    state = {"elapsed_s": 30.0, "telemetry_samples": samples}

    assert len(samples) < AUTO_UV_STALL_TUNING.selected_gpu_idle_min_samples
    assert selected_nvidia_gpu_idle_abort_reason(state) is None


def test_selected_gpu_idle_skips_when_no_util_or_power_values() -> None:
    samples = [
        TelemetrySample(elapsed_s=float(i), gpu_util_pct=None, power_w=None)
        for i in range(6, 20)
    ]
    state = {"elapsed_s": 30.0, "telemetry_samples": samples}

    assert selected_nvidia_gpu_idle_abort_reason(state) is None


def test_selected_gpu_idle_busy_power_blocks_idle_abort() -> None:
    # Utilisation looks idle but power is clearly above the idle ceiling.
    samples = [_sample(float(i), gpu_util_pct=2.0, power_w=120.0) for i in range(6, 20)]
    state = {"elapsed_s": 30.0, "telemetry_samples": samples}

    assert selected_nvidia_gpu_idle_abort_reason(state) is None


def test_selected_gpu_idle_reports_with_missing_power_values() -> None:
    # Util present and idle, power absent -> idle abort fires with power n/a.
    samples = [
        TelemetrySample(elapsed_s=float(i), gpu_util_pct=1.0, power_w=None)
        for i in range(6, 20)
    ]
    state = {"elapsed_s": 30.0, "telemetry_samples": samples}

    reason = selected_nvidia_gpu_idle_abort_reason(state)
    assert reason is not None
    assert reason.startswith("q2rtx-selected-nvidia-gpu-idle")
    assert "max_power=n/a" in reason
    assert "max_util=1.0%" in reason


# --------------------------------------------------------------------------
# load_lost_abort_reason (via telemetry_live_abort_reason) + low live clock
# --------------------------------------------------------------------------


def test_telemetry_abort_reports_load_lost_after_streak() -> None:
    # Idle-but-not-busy samples below the proper-run power floor for the
    # required streak length trigger a load-lost abort.
    streak = AUTO_UV_METRIC_TUNING.target_core_clock_low_streak_samples
    min_samples = AUTO_UV_STALL_TUNING.live_core_clock_abort_min_samples
    samples = [
        _sample(float(i), gpu_util_pct=10.0, power_w=20.0)
        for i in range(6, 6 + max(min_samples, 4))
    ]
    progress_state: dict = {}
    reason = None
    for _ in range(streak):
        reason = telemetry_live_abort_reason(
            {
                "elapsed_s": 30.0,
                "latest_sample": samples[-1],
                "telemetry_samples": samples,
            },
            busy_power_floor_w=60.0,
            proper_run_power_floor_w=40.0,
            target_core_clock_floor_mhz=None,
            progress_state=progress_state,
        )

    assert reason is not None
    assert reason.startswith("telemetry-live-load-lost")
    assert "current=20.0W" in reason
    assert "floor=40.0W" in reason
    assert "busy-floor=60.0W" in reason
    assert progress_state["low_power_streak"] == streak


def test_load_lost_streak_resets_when_sample_busy() -> None:
    min_samples = AUTO_UV_STALL_TUNING.live_core_clock_abort_min_samples
    busy = [
        _sample(float(i), gpu_util_pct=95.0, power_w=20.0)
        for i in range(6, 6 + max(min_samples, 4))
    ]
    progress_state = {"low_power_streak": 2}

    reason = telemetry_live_abort_reason(
        {
            "elapsed_s": 30.0,
            "latest_sample": busy[-1],
            "telemetry_samples": busy,
        },
        busy_power_floor_w=60.0,
        proper_run_power_floor_w=40.0,
        target_core_clock_floor_mhz=None,
        progress_state=progress_state,
    )

    # Busy sample (high util) resets the low-power streak and avoids abort.
    assert reason is None
    assert progress_state["low_power_streak"] == 0


def test_load_lost_skips_when_below_min_samples() -> None:
    samples = [_sample(6.0, gpu_util_pct=10.0, power_w=20.0)]
    progress_state: dict = {}

    reason = telemetry_live_abort_reason(
        {
            "elapsed_s": 30.0,
            "latest_sample": samples[-1],
            "telemetry_samples": samples,
        },
        busy_power_floor_w=60.0,
        proper_run_power_floor_w=40.0,
        target_core_clock_floor_mhz=None,
        progress_state=progress_state,
    )

    assert reason is None
    assert progress_state == {}


def test_telemetry_abort_reports_low_live_core_clock_after_streak() -> None:
    streak = AUTO_UV_METRIC_TUNING.target_core_clock_low_streak_samples
    min_samples = AUTO_UV_STALL_TUNING.live_core_clock_abort_min_samples
    # Busy samples (so load-lost stays inert) but with collapsed core clock.
    samples = [
        _sample(float(i), gpu_util_pct=95.0, power_w=120.0, core_clock_mhz=210.0)
        for i in range(6, 6 + max(min_samples, 4))
    ]
    progress_state: dict = {}
    reason = None
    for _ in range(streak):
        reason = telemetry_live_abort_reason(
            {
                "elapsed_s": 30.0,
                "latest_sample": samples[-1],
                "telemetry_samples": samples,
            },
            busy_power_floor_w=60.0,
            proper_run_power_floor_w=None,
            target_core_clock_floor_mhz=2000.0,
            progress_state=progress_state,
        )

    assert reason is not None
    assert reason.startswith("telemetry-live-core_clock ")
    assert "current=210.0MHz" in reason
    assert "floor=2000.0MHz" in reason
    assert progress_state["low_core_clock_streak"] == streak


def test_low_live_clock_skips_below_min_samples() -> None:
    progress_state: dict = {}
    reason = low_live_clock_abort_reason(
        live_core_clock_mhz=200.0,
        live_sample_is_busy=True,
        core_clock_samples=[200.0, 200.0],
        target_core_clock_floor_mhz=2000.0,
        progress_state=progress_state,
    )

    assert reason is None
    assert progress_state == {}


def test_low_live_clock_resets_streak_when_clock_healthy() -> None:
    min_samples = AUTO_UV_STALL_TUNING.live_core_clock_abort_min_samples
    progress_state = {"low_core_clock_streak": 2}

    reason = low_live_clock_abort_reason(
        live_core_clock_mhz=2400.0,
        live_sample_is_busy=True,
        core_clock_samples=[2400.0] * (min_samples + 1),
        target_core_clock_floor_mhz=2000.0,
        progress_state=progress_state,
    )

    assert reason is None
    assert progress_state["low_core_clock_streak"] == 0


def test_telemetry_abort_reports_avg_core_clock_below_floor() -> None:
    # Enough busy samples with a low average core clock, but the live streak
    # never accrues (avg path), so the average-clock rule fires.
    n = AUTO_UV_STALL_TUNING.avg_core_clock_abort_min_samples + 1
    samples = [
        _sample(float(i), gpu_util_pct=95.0, power_w=120.0, core_clock_mhz=210.0)
        for i in range(6, 6 + n)
    ]
    progress_state: dict = {}

    reason = telemetry_live_abort_reason(
        {
            "elapsed_s": 30.0,
            "latest_sample": samples[-1],
            "telemetry_samples": samples,
        },
        busy_power_floor_w=60.0,
        proper_run_power_floor_w=None,
        target_core_clock_floor_mhz=2000.0,
        progress_state=progress_state,
    )

    assert reason is not None
    assert reason.startswith("telemetry-live-core_clock-avg")
    assert "current=210.0MHz" in reason
    assert "floor=2000.0MHz" in reason


def test_telemetry_abort_passes_when_everything_healthy() -> None:
    n = AUTO_UV_STALL_TUNING.avg_core_clock_abort_min_samples + 1
    samples = [
        _sample(float(i), gpu_util_pct=95.0, power_w=120.0, core_clock_mhz=2400.0)
        for i in range(6, 6 + n)
    ]

    assert (
        telemetry_live_abort_reason(
            {
                "elapsed_s": 30.0,
                "latest_sample": samples[-1],
                "telemetry_samples": samples,
            },
            busy_power_floor_w=60.0,
            proper_run_power_floor_w=40.0,
            target_core_clock_floor_mhz=2000.0,
            progress_state={},
        )
        is None
    )


# --------------------------------------------------------------------------
# timedemo_stall_abort_reason
# --------------------------------------------------------------------------


def test_stall_skips_without_expected_loop_or_completed_runs() -> None:
    assert (
        timedemo_stall_abort_reason(
            {"completed_runs": 5},
            busy_power_floor_w=60.0,
            expected_loop_s=None,
            last_progress_elapsed_s=0.0,
        )
        is None
    )
    assert (
        timedemo_stall_abort_reason(
            {"completed_runs": 0},
            busy_power_floor_w=60.0,
            expected_loop_s=10.0,
            last_progress_elapsed_s=0.0,
        )
        is None
    )


def test_stall_skips_when_idle_within_threshold() -> None:
    assert (
        timedemo_stall_abort_reason(
            {"completed_runs": 3, "elapsed_s": 20.0},
            busy_power_floor_w=60.0,
            expected_loop_s=4.0,
            last_progress_elapsed_s=10.0,
        )
        is None
    )


def test_stall_aborts_when_idle_exceeds_threshold_and_gpu_quiet() -> None:
    latest = _sample(120.0, gpu_util_pct=5.0, power_w=10.0)
    reason = timedemo_stall_abort_reason(
        {"completed_runs": 4, "elapsed_s": 200.0, "latest_sample": latest},
        busy_power_floor_w=60.0,
        expected_loop_s=10.0,
        last_progress_elapsed_s=100.0,
    )

    assert reason is not None
    assert reason.startswith("timedemo-live-stall")
    assert "completed=4" in reason


def test_stall_suppressed_when_gpu_busy_by_util() -> None:
    latest = _sample(120.0, gpu_util_pct=95.0, power_w=10.0)
    assert (
        timedemo_stall_abort_reason(
            {"completed_runs": 4, "elapsed_s": 200.0, "latest_sample": latest},
            busy_power_floor_w=60.0,
            expected_loop_s=10.0,
            last_progress_elapsed_s=100.0,
        )
        is None
    )


def test_stall_suppressed_when_gpu_busy_by_power() -> None:
    latest = _sample(120.0, gpu_util_pct=5.0, power_w=120.0)
    assert (
        timedemo_stall_abort_reason(
            {"completed_runs": 4, "elapsed_s": 200.0, "latest_sample": latest},
            busy_power_floor_w=60.0,
            expected_loop_s=10.0,
            last_progress_elapsed_s=100.0,
        )
        is None
    )
