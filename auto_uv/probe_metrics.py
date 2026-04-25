from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from .models import AutoUvProbeSummary
from .tuning import AUTO_UV_CURVE_TUNING, AUTO_UV_METRIC_TUNING


def _mean(values: Sequence[float | int | None]) -> float | None:
    usable = [float(value) for value in values if value is not None]
    if not usable:
        return None
    return sum(usable) / len(usable)


def _max(values: Sequence[float | int | None]) -> float | None:
    usable = [float(value) for value in values if value is not None]
    if not usable:
        return None
    return max(usable)


def _sample_max(samples: list, attr: str) -> float | None:
    return _max(
        [
            getattr(sample, attr)
            for sample in samples
            if sample is not None and getattr(sample, attr, None) is not None
        ]
    )


def _percent(value: float | int) -> float:
    return max(0.0, float(value) / 100.0)


def _decision_samples(telemetry_samples: list) -> list:
    warmup_s = max(0.0, float(AUTO_UV_METRIC_TUNING.loaded_sample_warmup_s))
    if warmup_s <= 0.0:
        return list(telemetry_samples)
    filtered = [
        sample
        for sample in telemetry_samples
        if sample is not None
        and getattr(sample, "elapsed_s", None) is not None
        and float(sample.elapsed_s) >= warmup_s
    ]
    return filtered or list(telemetry_samples)


def _saturated_tail_samples(telemetry_samples: list) -> list:
    samples = _decision_samples(telemetry_samples)
    power_values = [
        float(sample.power_w)
        for sample in samples
        if sample is not None and getattr(sample, "power_w", None) is not None
    ]
    core_clock_values = [
        float(sample.core_clock_mhz)
        for sample in samples
        if sample is not None and getattr(sample, "core_clock_mhz", None) is not None
    ]
    if not power_values or not core_clock_values:
        return samples

    power_floor_w = max(power_values) * _percent(
        AUTO_UV_METRIC_TUNING.saturated_tail_power_pct
    )
    core_clock_floor_mhz = max(core_clock_values) * _percent(
        AUTO_UV_METRIC_TUNING.saturated_tail_core_clock_pct
    )

    def _is_saturated(sample) -> bool:
        return (
            sample is not None
            and getattr(sample, "power_w", None) is not None
            and getattr(sample, "core_clock_mhz", None) is not None
            and float(sample.power_w) >= float(power_floor_w)
            and float(sample.core_clock_mhz) >= float(core_clock_floor_mhz)
        )

    tail_reversed = []
    for sample in reversed(samples):
        if _is_saturated(sample):
            tail_reversed.append(sample)
            continue
        if tail_reversed:
            break
    tail = list(reversed(tail_reversed))
    min_samples = max(1, int(AUTO_UV_METRIC_TUNING.saturated_tail_min_samples))
    if len(tail) >= min_samples:
        return tail

    saturated = [sample for sample in samples if _is_saturated(sample)]
    if len(saturated) >= min_samples:
        return saturated
    return samples


def _derive_power_saturated_clock_mhz(
    telemetry_samples: list,
    *,
    power_limit_w: int | None,
) -> tuple[float | None, int, float | None]:
    if power_limit_w is None or int(power_limit_w) <= 0:
        return None, 0, None

    saturation_floor_w = float(power_limit_w) * (
        1.0 - _percent(AUTO_UV_METRIC_TUNING.power_saturation_headroom_pct)
    )
    telemetry_samples = _decision_samples(telemetry_samples)
    saturated_clocks = [
        float(sample.core_clock_mhz)
        for sample in telemetry_samples
        if sample is not None
        and sample.power_w is not None
        and sample.core_clock_mhz is not None
        and float(sample.power_w) >= saturation_floor_w
    ]
    if not saturated_clocks:
        return None, 0, float(saturation_floor_w)
    return (
        sum(saturated_clocks) / len(saturated_clocks),
        len(saturated_clocks),
        float(saturation_floor_w),
    )


def _derive_active_power_floor_w(
    telemetry_samples: list,
    *,
    power_limit_w: int | None,
    use_power_limit_floor: bool = False,
) -> float | None:
    telemetry_samples = _decision_samples(telemetry_samples)
    power_values = [
        float(sample.power_w)
        for sample in telemetry_samples
        if sample is not None and sample.power_w is not None
    ]
    if not power_values:
        return None

    max_power_w = max(power_values)
    if use_power_limit_floor and power_limit_w is not None and int(power_limit_w) > 0:
        return float(power_limit_w) * _percent(
            AUTO_UV_METRIC_TUNING.loaded_sample_power_floor_pct
        )
    return float(max_power_w) * _percent(
        AUTO_UV_METRIC_TUNING.loaded_sample_power_floor_pct
    )


def _derive_active_core_clock_mhz(
    telemetry_samples: list,
    *,
    power_limit_w: int | None,
    use_power_limit_floor: bool = False,
) -> tuple[float | None, float | None, int, float | None]:
    telemetry_samples = _decision_samples(telemetry_samples)
    active_power_floor_w = _derive_active_power_floor_w(
        telemetry_samples,
        power_limit_w=power_limit_w,
        use_power_limit_floor=use_power_limit_floor,
    )
    if active_power_floor_w is None:
        return None, None, 0, None

    active_clocks = sorted(
        float(sample.core_clock_mhz)
        for sample in telemetry_samples
        if sample is not None
        and sample.power_w is not None
        and sample.core_clock_mhz is not None
        and float(sample.power_w) >= float(active_power_floor_w)
    )
    if not active_clocks:
        return None, None, 0, float(active_power_floor_w)

    def _pick_percentile(values: list[float], percentile: float) -> float:
        if len(values) == 1:
            return float(values[0])
        position = int(round((len(values) - 1) * float(percentile)))
        position = max(0, min(len(values) - 1, position))
        return float(values[position])

    avg_clock_mhz = sum(active_clocks) / float(len(active_clocks))
    preferred_clock_mhz = _pick_percentile(
        active_clocks,
        AUTO_UV_METRIC_TUNING.active_core_clock_percentile,
    )
    return (
        float(avg_clock_mhz),
        float(preferred_clock_mhz),
        len(active_clocks),
        float(active_power_floor_w),
    )


def _derive_loaded_voltage_band_mv(
    telemetry_samples: list,
    *,
    power_limit_w: int | None,
    use_power_limit_floor: bool = False,
) -> tuple[int | None, int | None, int | None, int]:
    telemetry_samples = _decision_samples(telemetry_samples)
    active_power_floor_w = _derive_active_power_floor_w(
        telemetry_samples,
        power_limit_w=power_limit_w,
        use_power_limit_floor=use_power_limit_floor,
    )
    if active_power_floor_w is None:
        return None, None, None, 0

    active_voltages = sorted(
        int(round(float(sample.voltage_mv)))
        for sample in telemetry_samples
        if sample is not None
        and sample.power_w is not None
        and sample.voltage_mv is not None
        and float(sample.power_w) >= active_power_floor_w
    )
    if not active_voltages:
        return None, None, None, 0

    def _pick_percentile(values: list[int], percentile: float) -> int:
        if len(values) == 1:
            return int(values[0])
        position = int(round((len(values) - 1) * float(percentile)))
        position = max(0, min(len(values) - 1, position))
        return int(values[position])

    floor_mv = _pick_percentile(
        active_voltages,
        AUTO_UV_METRIC_TUNING.loaded_voltage_floor_percentile,
    )
    avg_mv = int(round(sum(active_voltages) / float(len(active_voltages))))
    ceil_mv = _pick_percentile(
        active_voltages,
        AUTO_UV_METRIC_TUNING.loaded_voltage_ceiling_percentile,
    )
    return int(floor_mv), int(avg_mv), int(ceil_mv), len(active_voltages)


def _derive_loaded_telemetry_means(
    telemetry_samples: list,
    *,
    power_limit_w: int | None,
    use_power_limit_floor: bool = False,
) -> tuple[
    float | None,
    float | None,
    float | None,
    float | None,
    float | None,
    int,
    float | None,
]:
    telemetry_samples = _decision_samples(telemetry_samples)
    active_power_floor_w = _derive_active_power_floor_w(
        telemetry_samples,
        power_limit_w=power_limit_w,
        use_power_limit_floor=use_power_limit_floor,
    )
    if active_power_floor_w is None:
        return None, None, None, None, None, 0, None

    active_samples = [
        sample
        for sample in telemetry_samples
        if sample is not None
        and sample.power_w is not None
        and float(sample.power_w) >= float(active_power_floor_w)
    ]
    if not active_samples:
        return None, None, None, None, None, 0, float(active_power_floor_w)

    active_power_values = [
        float(sample.power_w) for sample in active_samples if sample.power_w is not None
    ]
    active_clock_values = [
        float(sample.core_clock_mhz)
        for sample in active_samples
        if sample.core_clock_mhz is not None
    ]
    active_voltage_values = [
        float(sample.voltage_mv)
        for sample in active_samples
        if sample.voltage_mv is not None
    ]
    active_temperature_values = [
        float(sample.temperature_c)
        for sample in active_samples
        if sample.temperature_c is not None
    ]
    active_fan_values = [
        float(sample.fan_speed_pct)
        for sample in active_samples
        if sample.fan_speed_pct is not None
    ]
    return (
        _mean(active_power_values),
        _mean(active_clock_values),
        _mean(active_voltage_values),
        _mean(active_temperature_values),
        _mean(active_fan_values),
        len(active_samples),
        float(active_power_floor_w),
    )


def _history_average(history: list[AutoUvProbeSummary], attr: str) -> float | None:
    return _mean([getattr(item, attr) for item in history])


def _baseline_value(history: list[AutoUvProbeSummary], attr: str) -> float | int | None:
    if not history:
        return None
    return getattr(history[0], attr)


def _latest_non_companion_probe(
    history: list[AutoUvProbeSummary],
) -> AutoUvProbeSummary | None:
    for probe in reversed(history):
        if not bool(probe.used_companion_load):
            return probe
    return history[-1] if history else None


def _temperature_normalized_power_w(
    probe: AutoUvProbeSummary | None,
    *,
    reference_temperature_c: float | None,
) -> float | None:
    if probe is None or probe.avg_power_w is None:
        return None
    if reference_temperature_c is None or probe.avg_temperature_c is None:
        return float(probe.avg_power_w)
    max_delta_c = max(
        0.0, float(AUTO_UV_METRIC_TUNING.temperature_normalization_max_delta_c)
    )
    delta_c = max(
        -max_delta_c,
        min(
            max_delta_c, float(probe.avg_temperature_c) - float(reference_temperature_c)
        ),
    )
    correction = 1.0 + (
        float(AUTO_UV_METRIC_TUNING.temperature_normalization_power_pct_per_c)
        / 100.0
        * float(delta_c)
    )
    if correction <= 0.0:
        return float(probe.avg_power_w)
    return float(probe.avg_power_w) / correction


def _temperature_normalized_fps_per_w(
    probe: AutoUvProbeSummary | None,
    *,
    reference_temperature_c: float | None,
) -> float | None:
    normalized_power_w = _temperature_normalized_power_w(
        probe,
        reference_temperature_c=reference_temperature_c,
    )
    if probe is None or normalized_power_w in (None, 0.0):
        return None
    if probe.avg_fps is not None:
        return float(probe.avg_fps) / float(normalized_power_w)
    if probe.efficiency_fps_per_w is not None and probe.avg_power_w is not None:
        return (
            float(probe.efficiency_fps_per_w)
            * float(probe.avg_power_w)
            / float(normalized_power_w)
        )
    return None


def _temperature_normalized_comparison(
    previous_probe: AutoUvProbeSummary | None,
    candidate_probe: AutoUvProbeSummary | None,
) -> dict[str, float | None]:
    reference_temperature_c = (
        float(previous_probe.avg_temperature_c)
        if previous_probe is not None and previous_probe.avg_temperature_c is not None
        else (
            float(candidate_probe.avg_temperature_c)
            if candidate_probe is not None
            and candidate_probe.avg_temperature_c is not None
            else None
        )
    )
    return {
        "reference_temperature_c": reference_temperature_c,
        "previous_power_w": _temperature_normalized_power_w(
            previous_probe,
            reference_temperature_c=reference_temperature_c,
        ),
        "candidate_power_w": _temperature_normalized_power_w(
            candidate_probe,
            reference_temperature_c=reference_temperature_c,
        ),
        "previous_fps_per_w": _temperature_normalized_fps_per_w(
            previous_probe,
            reference_temperature_c=reference_temperature_c,
        ),
        "candidate_fps_per_w": _temperature_normalized_fps_per_w(
            candidate_probe,
            reference_temperature_c=reference_temperature_c,
        ),
    }


def _temperature_normalized_efficiency_delta(
    previous_probe: AutoUvProbeSummary | None,
    candidate_probe: AutoUvProbeSummary | None,
) -> dict[str, float | bool | None]:
    normalized = _temperature_normalized_comparison(previous_probe, candidate_probe)
    previous_measured_voltage_mv = (
        previous_probe.avg_voltage_mv
        if previous_probe is not None and previous_probe.avg_voltage_mv is not None
        else (
            previous_probe.live_voltage_after_mv if previous_probe is not None else None
        )
    )
    candidate_measured_voltage_mv = (
        candidate_probe.avg_voltage_mv
        if candidate_probe is not None and candidate_probe.avg_voltage_mv is not None
        else (
            candidate_probe.live_voltage_after_mv
            if candidate_probe is not None
            else None
        )
    )
    measured_voltage_drop_mv = None
    if (
        previous_measured_voltage_mv is not None
        and candidate_measured_voltage_mv is not None
    ):
        measured_voltage_drop_mv = float(previous_measured_voltage_mv) - float(
            candidate_measured_voltage_mv
        )
    requested_voltage_drop_mv = None
    if previous_probe is not None and candidate_probe is not None:
        requested_voltage_drop_mv = float(previous_probe.candidate_voltage_mv) - float(
            candidate_probe.candidate_voltage_mv
        )
    measured_voltage_close_to_requested = True
    if (
        requested_voltage_drop_mv is not None
        and requested_voltage_drop_mv > 0.0
        and measured_voltage_drop_mv is not None
    ):
        measured_voltage_close_to_requested = (
            abs(float(measured_voltage_drop_mv))
            >= abs(float(requested_voltage_drop_mv)) * 0.5
        )
    previous_eff = normalized["previous_fps_per_w"]
    candidate_eff = normalized["candidate_fps_per_w"]
    if previous_eff in (None, 0.0) or candidate_eff is None:
        return {
            **normalized,
            "previous_measured_voltage_mv": previous_measured_voltage_mv,
            "candidate_measured_voltage_mv": candidate_measured_voltage_mv,
            "measured_voltage_drop_mv": measured_voltage_drop_mv,
            "requested_voltage_drop_mv": requested_voltage_drop_mv,
            "measured_voltage_close_to_requested": bool(
                measured_voltage_close_to_requested
            ),
            "delta_fps_per_w": None,
            "delta_pct": None,
            "improved": None,
        }
    delta = float(candidate_eff) - float(previous_eff)
    delta_pct = (float(delta) / float(previous_eff)) * 100.0
    improved = float(delta_pct) > float(
        AUTO_UV_METRIC_TUNING.min_temp_normalized_fps_per_w_improvement_pct
    )
    return {
        **normalized,
        "previous_measured_voltage_mv": previous_measured_voltage_mv,
        "candidate_measured_voltage_mv": candidate_measured_voltage_mv,
        "measured_voltage_drop_mv": measured_voltage_drop_mv,
        "requested_voltage_drop_mv": requested_voltage_drop_mv,
        "measured_voltage_close_to_requested": bool(
            measured_voltage_close_to_requested
        ),
        "delta_fps_per_w": float(delta),
        "delta_pct": float(delta_pct),
        "improved": bool(improved),
    }


def _summarize_probe(
    *,
    candidate_voltage_mv: int,
    lock_clock_mhz: int,
    live_voltage_before_mv: int | None,
    live_voltage_after_mv: int | None,
    used_companion_load: bool,
    power_limit_w: int | None,
    result,
    telemetry_samples: list | None = None,
    use_power_limit_floor: bool = False,
) -> AutoUvProbeSummary:
    fps_values = [float(run.fps) for run in result.timedemo_runs]
    frame_values = [int(run.frames) for run in result.timedemo_runs]
    q2rtx_samples = (
        list(telemetry_samples)
        if telemetry_samples is not None
        else list(result.telemetry_samples)
    )
    telemetry = result.telemetry_summary()
    if telemetry_samples is not None:
        telemetry = {
            "sample_count": len(q2rtx_samples),
            "power_max": _sample_max(q2rtx_samples, "power_w"),
            "temperature_max": _sample_max(q2rtx_samples, "temperature_c"),
            "fan_max": _sample_max(q2rtx_samples, "fan_speed_pct"),
        }
    companion_samples = list(getattr(result, "companion_telemetry_samples", []) or [])
    max_power_w = _max(
        [
            telemetry.get("power_max"),
            _sample_max(companion_samples, "power_w"),
        ]
    )
    max_temperature_c = _max(
        [
            telemetry.get("temperature_max"),
            _sample_max(companion_samples, "temperature_c"),
        ]
    )
    max_fan_speed_pct = _max(
        [
            telemetry.get("fan_max"),
            _sample_max(companion_samples, "fan_speed_pct"),
        ]
    )
    (
        loaded_power_w,
        loaded_clock_mhz,
        loaded_voltage_mv,
        loaded_temperature_c,
        loaded_fan_speed_pct,
        _,
        _,
    ) = _derive_loaded_telemetry_means(
        q2rtx_samples,
        power_limit_w=power_limit_w,
        use_power_limit_floor=use_power_limit_floor,
    )
    summary_voltage_mv = (
        float(loaded_voltage_mv)
        if loaded_voltage_mv is not None
        else (
            float(telemetry["voltage_avg"])
            if telemetry.get("voltage_avg") is not None
            else None
        )
    )
    summary_power_w = (
        float(loaded_power_w)
        if loaded_power_w is not None
        else (
            float(telemetry["power_avg"])
            if telemetry.get("power_avg") is not None
            else None
        )
    )
    summary_clock_mhz = (
        float(loaded_clock_mhz)
        if loaded_clock_mhz is not None
        else (
            float(telemetry["core_clock_avg"])
            if telemetry.get("core_clock_avg") is not None
            else None
        )
    )
    summary_temperature_c = (
        float(loaded_temperature_c)
        if loaded_temperature_c is not None
        else (
            float(telemetry["temperature_avg"])
            if telemetry.get("temperature_avg") is not None
            else None
        )
    )
    summary_fan_speed_pct = (
        float(loaded_fan_speed_pct)
        if loaded_fan_speed_pct is not None
        else (
            float(telemetry["fan_avg"])
            if telemetry.get("fan_avg") is not None
            else None
        )
    )
    avg_fps = _mean(fps_values)
    return AutoUvProbeSummary(
        candidate_voltage_mv=int(candidate_voltage_mv),
        lock_clock_mhz=int(lock_clock_mhz),
        live_voltage_before_mv=live_voltage_before_mv,
        live_voltage_after_mv=live_voltage_after_mv,
        avg_voltage_mv=summary_voltage_mv,
        frames_per_run=frame_values[0] if frame_values else None,
        avg_seconds_per_run=_mean([float(run.seconds) for run in result.timedemo_runs]),
        avg_fps=avg_fps,
        min_fps=min(fps_values) if fps_values else None,
        max_fps=max(fps_values) if fps_values else None,
        avg_power_w=summary_power_w,
        max_power_w=(float(max_power_w) if max_power_w is not None else None),
        avg_temperature_c=summary_temperature_c,
        max_temperature_c=(
            float(max_temperature_c) if max_temperature_c is not None else None
        ),
        avg_fan_speed_pct=summary_fan_speed_pct,
        max_fan_speed_pct=(
            float(max_fan_speed_pct) if max_fan_speed_pct is not None else None
        ),
        avg_core_clock_mhz=summary_clock_mhz,
        efficiency_fps_per_w=(
            avg_fps / float(summary_power_w)
            if avg_fps is not None
            and summary_power_w not in (None, 0, 0.0)
            and fps_values
            else None
        ),
        efficiency_mhz_per_w=(
            float(summary_clock_mhz) / float(summary_power_w)
            if summary_clock_mhz is not None and summary_power_w not in (None, 0, 0.0)
            else None
        ),
        watts_per_mhz=(
            float(summary_power_w) / float(summary_clock_mhz)
            if summary_clock_mhz not in (None, 0, 0.0) and summary_power_w is not None
            else None
        ),
        used_companion_load=bool(used_companion_load),
        result_reason=str(result.reason),
        log_path=Path(result.log_path),
    )


def _evaluate_probe(
    probe: AutoUvProbeSummary,
    *,
    stable_history: list[AutoUvProbeSummary],
    min_performance_core_clock_pct: float | None = None,
) -> str:
    if not stable_history:
        return ""

    baseline_frames = stable_history[0].frames_per_run
    if baseline_frames is not None and probe.frames_per_run != baseline_frames:
        return (
            f"frame-count-regression current={probe.frames_per_run} "
            f"baseline={baseline_frames}"
        )

    baseline_fps = _baseline_value(stable_history, "avg_fps")
    if (
        baseline_fps is not None
        and probe.avg_fps is not None
        and probe.avg_fps
        < baseline_fps * _percent(AUTO_UV_METRIC_TUNING.min_proper_run_fps_pct)
    ):
        floor_fps = baseline_fps * _percent(
            AUTO_UV_METRIC_TUNING.min_proper_run_fps_pct
        )
        return (
            f"fps-regression current={probe.avg_fps:.1f} "
            f"baseline={baseline_fps:.1f} floor={floor_fps:.1f} "
            f"margin={AUTO_UV_METRIC_TUNING.min_proper_run_fps_pct:.1f}%"
        )

    baseline_avg_core_clock = _baseline_value(stable_history, "avg_core_clock_mhz")
    if min_performance_core_clock_pct is None:
        min_performance_core_clock_pct = (
            AUTO_UV_METRIC_TUNING.min_performance_core_clock_pct
        )
    if (
        baseline_avg_core_clock is not None
        and probe.avg_core_clock_mhz is not None
        and probe.avg_core_clock_mhz
        < (
            baseline_avg_core_clock * _percent(float(min_performance_core_clock_pct))
            - AUTO_UV_CURVE_TUNING.clock_select_tolerance_mhz
        )
    ):
        floor_core_clock = baseline_avg_core_clock * _percent(
            float(min_performance_core_clock_pct)
        )
        return (
            f"core_clock-regression current={probe.avg_core_clock_mhz:.1f}MHz "
            f"baseline={baseline_avg_core_clock:.1f}MHz floor={floor_core_clock:.1f}MHz "
            f"tolerance={AUTO_UV_CURVE_TUNING.clock_select_tolerance_mhz:.1f}MHz"
        )

    return ""
