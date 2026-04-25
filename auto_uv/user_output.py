from __future__ import annotations

from pathlib import Path
from typing import Callable

from ascii_chart import render_line_chart

from .models import AutoUvProbeSummary
from .probe_metrics import (
    _temperature_normalized_comparison,
    _temperature_normalized_fps_per_w,
    _temperature_normalized_power_w,
)


def log_phase(
    log: Callable[[str], None],
    phase: str,
    message: str,
) -> None:
    log(f"Auto-UV phase={phase} {message}")


def format_probe_summary(probe: AutoUvProbeSummary) -> str:
    parts = [
        f"candidate={probe.candidate_voltage_mv}mV",
        f"target={probe.lock_clock_mhz}MHz",
    ]
    if probe.live_voltage_before_mv is not None:
        parts.append(f"live-before={probe.live_voltage_before_mv}mV")
    if probe.live_voltage_after_mv is not None:
        parts.append(f"live-after={probe.live_voltage_after_mv}mV")
    if probe.avg_voltage_mv is not None:
        parts.append(f"avg_voltage={probe.avg_voltage_mv:.1f}mV")
    if probe.frames_per_run is not None:
        parts.append(f"frames={probe.frames_per_run}")
    if probe.avg_seconds_per_run is not None:
        parts.append(f"loop={probe.avg_seconds_per_run:.2f}s")
    if probe.avg_fps is not None:
        parts.append(f"fps={probe.avg_fps:.1f}")
    if probe.avg_power_w is not None:
        parts.append(f"power={probe.avg_power_w:.1f}W")
    if probe.max_power_w is not None:
        parts.append(f"power_max={probe.max_power_w:.1f}W")
    if probe.avg_temperature_c is not None:
        parts.append(f"temp={probe.avg_temperature_c:.1f}C")
    if probe.max_temperature_c is not None:
        parts.append(f"temp_max={probe.max_temperature_c:.1f}C")
    if probe.avg_fan_speed_pct is not None:
        parts.append(f"fan={probe.avg_fan_speed_pct:.1f}%")
    if probe.max_fan_speed_pct is not None:
        parts.append(f"fan_max={probe.max_fan_speed_pct:.1f}%")
    if probe.avg_core_clock_mhz is not None:
        parts.append(f"core_clock={probe.avg_core_clock_mhz:.1f}MHz")
    if probe.efficiency_mhz_per_w is not None:
        parts.append(f"eff={probe.efficiency_mhz_per_w:.3f}MHz/W")
    return " ".join(parts)


def format_benchmark_delta(
    current_value: float | None,
    reference_value: float | None,
    *,
    higher_is_better: bool,
    unit: str,
) -> str:
    if current_value is None or reference_value is None:
        return "n/a"
    delta = float(current_value) - float(reference_value)
    pct = (
        (delta / float(reference_value) * 100.0)
        if float(reference_value) != 0.0
        else 0.0
    )
    direction = (
        "better" if (delta > 0.0 if higher_is_better else delta < 0.0) else "worse"
    )
    if abs(delta) < 1e-9:
        direction = "same"
    return f"{delta:+.5f}{unit} ({pct:+.2f}%, {direction})"


def log_benchmark(
    log: Callable[[str], None],
    *,
    phase: str,
    probe: AutoUvProbeSummary,
    reference_probe: AutoUvProbeSummary | None = None,
    reference_label: str = "initial",
) -> None:
    parts = []
    if probe.efficiency_fps_per_w is not None:
        parts.append(f"fps_per_w={probe.efficiency_fps_per_w:.5f}FPS/W")
    if probe.efficiency_mhz_per_w is not None:
        parts.append(f"mhz_per_w={probe.efficiency_mhz_per_w:.5f}MHz/W")
    if reference_probe is not None:
        parts.append(
            f"vs-{reference_label}-fps_per_w="
            + format_benchmark_delta(
                probe.efficiency_fps_per_w,
                reference_probe.efficiency_fps_per_w,
                higher_is_better=True,
                unit="FPS/W",
            )
        )
        parts.append(
            f"vs-{reference_label}-mhz_per_w="
            + format_benchmark_delta(
                probe.efficiency_mhz_per_w,
                reference_probe.efficiency_mhz_per_w,
                higher_is_better=True,
                unit="MHz/W",
            )
        )
        normalized = _temperature_normalized_comparison(reference_probe, probe)
        reference_temp_c = normalized["reference_temperature_c"]
        if (
            reference_temp_c is not None
            and normalized["candidate_fps_per_w"] is not None
            and normalized["previous_fps_per_w"] is not None
        ):
            parts.append(
                f"vs-{reference_label}-temp_norm_fps_per_w@{float(reference_temp_c):.1f}C="
                + format_benchmark_delta(
                    normalized["candidate_fps_per_w"],
                    normalized["previous_fps_per_w"],
                    higher_is_better=True,
                    unit="FPS/W",
                )
            )
        if (
            reference_temp_c is not None
            and normalized["candidate_power_w"] is not None
            and normalized["previous_power_w"] is not None
        ):
            parts.append(
                f"vs-{reference_label}-temp_norm_power@{float(reference_temp_c):.1f}C="
                + format_benchmark_delta(
                    normalized["candidate_power_w"],
                    normalized["previous_power_w"],
                    higher_is_better=False,
                    unit="W",
                )
            )
    log_phase(log, phase, "benchmark " + " ".join(parts))


def log_vf_ascii_chart(
    log: Callable[[str], None],
    *,
    plan: list[dict],
    target_clock_mhz: int,
    candidate_voltage_mv: int,
) -> None:
    if not plan:
        return

    all_points = [
        (float(item["voltage_mv"]), float(item["base_mhz"]), float(item["target_mhz"]))
        for item in plan
    ]
    if not all_points:
        return

    min_voltage_mv = min(point[0] for point in all_points)
    max_voltage_mv = max(point[0] for point in all_points)
    voltage_cutoff_mv = min_voltage_mv + (max_voltage_mv - min_voltage_mv) * 0.5

    def _filtered_points(key: str) -> list[tuple[float, float]]:
        points = []
        for item in plan:
            voltage_mv = float(item["voltage_mv"])
            clock_mhz = float(item[key])
            if voltage_mv < float(voltage_cutoff_mv):
                continue
            points.append((voltage_mv, clock_mhz))
        return sorted(points)

    stock_points = _filtered_points("base_mhz")
    target_points = _filtered_points("target_mhz")
    if not stock_points and not target_points:
        stock_points = sorted(
            [
                (float(item["voltage_mv"]), float(item["base_mhz"]))
                for item in plan
                if float(item["voltage_mv"]) >= float(voltage_cutoff_mv)
            ]
        )
        target_points = sorted(
            [
                (float(item["voltage_mv"]), float(item["target_mhz"]))
                for item in plan
                if float(item["voltage_mv"]) >= float(voltage_cutoff_mv)
            ]
        )

    vf_series = [
        {
            "name": "stock",
            "char": ".",
            "points": stock_points,
        },
        {
            "name": "target",
            "char": "#",
            "points": target_points,
        },
    ]
    for line in render_line_chart(
        ("VF curve zoom (top 50% voltage range, target=# stock=. lock=@, x=mV y=MHz)"),
        series=vf_series,
        x_label="mV",
        y_label="MHz",
        x_rounding=50,
        y_rounding=100,
        highlights=[
            {
                "x": float(candidate_voltage_mv),
                "y": float(target_clock_mhz),
                "char": "@",
            }
        ],
    ):
        log(line)


def log_vf_point_list(
    log: Callable[[str], None],
    *,
    plan: list[dict],
    label: str,
) -> None:
    if not plan:
        return
    rendered = " ".join(
        f"{int(item['voltage_mv'])}:{int(item['target_mhz'])}"
        for item in sorted(plan, key=lambda entry: int(entry["voltage_mv"]))
    )
    log(f"Auto-UV phase=points {label} {rendered}")


def log_fan_curve_ascii_chart(
    log: Callable[[str], None],
    *,
    curve: list[list[float]] | list[tuple[float, float]],
    loaded_temperature_c: float | int | None = None,
    load_anchor_fan_speed_pct: float | int | None = None,
) -> None:
    if not curve:
        return

    points = sorted((float(point[0]), float(point[1])) for point in curve)
    highlights = []
    if loaded_temperature_c is not None and load_anchor_fan_speed_pct is not None:
        highlights.append(
            {
                "x": float(loaded_temperature_c),
                "y": float(load_anchor_fan_speed_pct),
                "char": "@",
            }
        )

    for line in render_line_chart(
        "Auto-UV fan curve (target=# load=@, x=C y=% fan)",
        series=[
            {
                "name": "target",
                "char": "#",
                "points": points,
            }
        ],
        x_label="C",
        y_label="%",
        x_rounding=10,
        y_rounding=10,
        include_zero_y=True,
        highlights=highlights,
    ):
        log(line)


def log_final_summary(
    log: Callable[[str], None],
    *,
    baseline_probe: AutoUvProbeSummary | None,
    final_probe: AutoUvProbeSummary | None,
    final_voltage_mv: int,
    final_lock_clock_mhz: int,
    clock_drop_margin_pct: float,
    final_verification_status: str | None = None,
    final_curve_overclock: dict | None = None,
) -> None:
    if baseline_probe is None or final_probe is None:
        return
    baseline_clock = baseline_probe.avg_core_clock_mhz
    final_clock = final_probe.avg_core_clock_mhz
    baseline_power = baseline_probe.avg_power_w
    final_power = final_probe.avg_power_w
    baseline_temp = baseline_probe.avg_temperature_c
    final_temp = final_probe.avg_temperature_c
    baseline_fan = baseline_probe.avg_fan_speed_pct
    final_fan = final_probe.avg_fan_speed_pct
    baseline_eff = baseline_probe.efficiency_mhz_per_w
    final_eff = final_probe.efficiency_mhz_per_w

    clock_drop_mhz = (
        float(baseline_clock) - float(final_clock)
        if baseline_clock is not None and final_clock is not None
        else None
    )
    clock_drop_pct = (
        (float(clock_drop_mhz) / float(baseline_clock)) * 100.0
        if clock_drop_mhz is not None and baseline_clock not in (None, 0.0)
        else None
    )
    power_saved_w = (
        float(baseline_power) - float(final_power)
        if baseline_power is not None and final_power is not None
        else None
    )
    power_saved_pct = (
        (float(power_saved_w) / float(baseline_power)) * 100.0
        if power_saved_w is not None and baseline_power not in (None, 0.0)
        else None
    )
    eff_gain_pct = (
        ((float(final_eff) / float(baseline_eff)) - 1.0) * 100.0
        if final_eff is not None and baseline_eff not in (None, 0.0)
        else None
    )
    within_margin = clock_drop_pct is not None and float(clock_drop_pct) <= float(
        clock_drop_margin_pct
    )

    def _metric(value: float | int | None, *, suffix: str, precision: int = 1) -> str:
        if value is None:
            return "n/a"
        return f"{float(value):.{int(precision)}f}{suffix}"

    log_phase(
        log,
        "summary",
        f"final={final_lock_clock_mhz}MHz@{final_voltage_mv}mV "
        f"start-core_clock={_metric(baseline_clock, suffix='MHz')} "
        f"final-core_clock={_metric(final_clock, suffix='MHz')} "
        f"clock-drop={_metric(clock_drop_mhz, suffix='MHz')}/"
        f"{_metric(clock_drop_pct, suffix='%', precision=2)} "
        f"margin={float(clock_drop_margin_pct):.1f}% "
        f"margin-ok={'yes' if within_margin else 'no'}",
    )
    log_phase(
        log,
        "summary",
        f"start-power={_metric(baseline_power, suffix='W')} "
        f"final-power={_metric(final_power, suffix='W')} "
        f"saved={_metric(power_saved_w, suffix='W')}/"
        f"{_metric(power_saved_pct, suffix='%', precision=2)} "
        f"start-temp={_metric(baseline_temp, suffix='C')} "
        f"final-temp={_metric(final_temp, suffix='C')} "
        f"start-fan={_metric(baseline_fan, suffix='%')} "
        f"final-fan={_metric(final_fan, suffix='%')} "
        f"start-eff={_metric(baseline_eff, suffix='MHz/W', precision=5)} "
        f"final-eff={_metric(final_eff, suffix='MHz/W', precision=5)} "
        f"eff-gain={_metric(eff_gain_pct, suffix='%', precision=2)}",
    )
    if final_curve_overclock is not None:
        lock_offset = final_curve_overclock.get("lock_offset_mhz")
        lock_vanilla = final_curve_overclock.get("lock_vanilla_mhz")
        lock_final = final_curve_overclock.get("lock_final_mhz")
        log_phase(
            log,
            "summary",
            "curve-overclock-vs-vanilla "
            f"lock={_metric(lock_offset, suffix='MHz', precision=0)} "
            f"vanilla={_metric(lock_vanilla, suffix='MHz', precision=0)} "
            f"final={_metric(lock_final, suffix='MHz', precision=0)} "
            f"voltage={final_curve_overclock.get('lock_voltage_mv')}mV "
            f"range={_metric(final_curve_overclock.get('min_offset_mhz'), suffix='MHz', precision=0)}.."
            f"{_metric(final_curve_overclock.get('max_offset_mhz'), suffix='MHz', precision=0)} "
            f"avg={_metric(final_curve_overclock.get('avg_offset_mhz'), suffix='MHz', precision=1)} "
            f"points={final_curve_overclock.get('positive_points')}/{final_curve_overclock.get('total_points')}",
        )
    if final_verification_status:
        log_phase(log, "summary", f"final-verification={final_verification_status}")


def format_user_value(
    value: float | int | None, suffix: str = "", *, precision: int = 1
) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.{int(precision)}f}{suffix}"


def format_user_change(
    before: float | int | None,
    after: float | int | None,
    suffix: str = "",
    *,
    precision: int = 1,
    lower_is_better: bool = False,
) -> str:
    if before is None or after is None:
        return "n/a"
    delta = float(after) - float(before)
    pct = (delta / float(before) * 100.0) if float(before) != 0.0 else None
    sign = "+" if delta > 0.0 else ""
    if pct is None:
        return f"{sign}{delta:.{int(precision)}f}{suffix}"
    if lower_is_better:
        saved = -delta
        saved_pct = -pct
        if saved >= 0.0:
            return f"{saved:.{int(precision)}f}{suffix} lower ({saved_pct:.2f}% lower)"
        return f"{abs(saved):.{int(precision)}f}{suffix} higher ({abs(saved_pct):.2f}% higher)"
    return f"{sign}{delta:.{int(precision)}f}{suffix} ({pct:+.2f}%)"


def log_user_table(
    log: Callable[[str], None],
    *,
    title: str,
    headers: tuple[str, ...],
    rows: list[tuple[str, ...]],
) -> None:
    if not rows:
        return
    widths = [
        max(len(str(item)) for item in [header, *(row[index] for row in rows)])
        for index, header in enumerate(headers)
    ]

    def _render_row(values: tuple[str, ...]) -> str:
        return (
            "| "
            + " | ".join(
                str(value).ljust(widths[index]) for index, value in enumerate(values)
            )
            + " |"
        )

    separator = "|-" + "-|-".join("-" * width for width in widths) + "-|"
    log(title)
    log(_render_row(headers))
    log(separator)
    for row in rows:
        log(_render_row(row))


def log_user_stage(
    log: Callable[[str], None],
    title: str,
    lines: list[str],
) -> None:
    log("")
    log(f"Auto-UV: {title}")
    for line in lines:
        log(f"  {line}")


def log_user_candidate_intro(
    log: Callable[[str], None],
    *,
    attempt: int,
    stable_voltage_mv: int,
    stable_lock_clock_mhz: int,
    candidate_voltage_mv: int,
    candidate_lock_clock_mhz: int,
    start_voltage_mv: int,
    min_search_voltage_mv: int,
    phase: str,
) -> None:
    voltage_drop_pct = (
        (1.0 - (float(candidate_voltage_mv) / float(start_voltage_mv))) * 100.0
        if int(start_voltage_mv) > 0
        else 0.0
    )
    log_user_stage(
        log,
        f"Step {int(attempt)} - testing a lower voltage",
        [
            f"Current best stable curve: {int(stable_lock_clock_mhz)}MHz at {int(stable_voltage_mv)}mV.",
            f"Trying now: {int(candidate_lock_clock_mhz)}MHz at {int(candidate_voltage_mv)}mV.",
            f"This is {voltage_drop_pct:.1f}% below the starting voltage; scan floor is {int(min_search_voltage_mv)}mV.",
            f"Probe tier: {phase}. PenguinBurner is applying this curve, running Q2RTX/CUDA, and watching clocks, power, temperature, and fan speed.",
        ],
    )


def log_user_candidate_result(
    log: Callable[[str], None],
    *,
    attempt: int,
    decision: str,
    reason: str,
    initial_probe: AutoUvProbeSummary | None = None,
    previous_probe: AutoUvProbeSummary | None,
    candidate_probe: AutoUvProbeSummary,
    restored_voltage_mv: int | None = None,
    restored_lock_clock_mhz: int | None = None,
) -> None:
    title = f"Step {int(attempt)} result - {decision}"
    lines = [reason]
    if restored_voltage_mv is not None and restored_lock_clock_mhz is not None:
        lines.append(
            f"Using stable curve: {int(restored_lock_clock_mhz)}MHz at {int(restored_voltage_mv)}mV."
        )
    log_user_stage(log, title, lines)

    def _voltage(probe: AutoUvProbeSummary | None) -> str:
        return f"{int(probe.candidate_voltage_mv)}mV" if probe is not None else "n/a"

    def _live_voltage_after(probe: AutoUvProbeSummary | None) -> str:
        if probe is None or probe.live_voltage_after_mv is None:
            return "n/a"
        return f"{int(probe.live_voltage_after_mv)}mV"

    def _target_clock(probe: AutoUvProbeSummary | None) -> str:
        return f"{int(probe.lock_clock_mhz)}MHz" if probe is not None else "n/a"

    def _initial_measured_clock_mhz() -> int | None:
        if initial_probe is None or initial_probe.avg_core_clock_mhz is None:
            return None
        return int(round(float(initial_probe.avg_core_clock_mhz)))

    def _initial_clock() -> str:
        measured_clock_mhz = _initial_measured_clock_mhz()
        return f"{measured_clock_mhz}MHz" if measured_clock_mhz is not None else "n/a"

    def _fps_per_w(probe: AutoUvProbeSummary | None) -> str:
        if probe is None:
            return "n/a"
        if probe.efficiency_fps_per_w is not None:
            return format_user_value(
                probe.efficiency_fps_per_w,
                "FPS/W",
                precision=5,
            )
        if probe.avg_power_w is not None and probe.frames_per_run is None:
            return f"n/a ({probe.result_reason}; no completed timedemo)"
        return "n/a"

    def _signed_int_change(
        reference_value: int | float | None,
        candidate_value: int | float | None,
        unit: str,
    ) -> str:
        if reference_value is None or candidate_value is None:
            return "n/a"
        return f"{int(candidate_value) - int(reference_value):+d}{unit}"

    def _change_vs_initial(
        initial_value: float | int | None,
        candidate_value: float | int | None,
        unit: str,
        *,
        precision: int = 1,
        lower_is_better: bool = False,
    ) -> str:
        return format_user_change(
            initial_value,
            candidate_value,
            unit,
            precision=precision,
            lower_is_better=lower_is_better,
        )

    temperature_normalized_rows: list[tuple[str, str, str, str, str, str]] = []
    if previous_probe is not None:
        normalized = _temperature_normalized_comparison(previous_probe, candidate_probe)
        reference_temp_c = normalized["reference_temperature_c"]
        if (
            reference_temp_c is not None
            and normalized["previous_power_w"] is not None
            and normalized["candidate_power_w"] is not None
            and normalized["previous_fps_per_w"] is not None
            and normalized["candidate_fps_per_w"] is not None
        ):
            initial_norm_power = _temperature_normalized_power_w(
                initial_probe,
                reference_temperature_c=float(reference_temp_c),
            )
            initial_norm_fps_w = _temperature_normalized_fps_per_w(
                initial_probe,
                reference_temperature_c=float(reference_temp_c),
            )
            temperature_normalized_rows = [
                (
                    f"Power @ {float(reference_temp_c):.1f}C",
                    format_user_value(initial_norm_power, "W"),
                    format_user_value(normalized["previous_power_w"], "W"),
                    format_user_value(normalized["candidate_power_w"], "W"),
                    format_user_change(
                        normalized["previous_power_w"],
                        normalized["candidate_power_w"],
                        "W",
                        lower_is_better=True,
                    ),
                    _change_vs_initial(
                        initial_norm_power,
                        normalized["candidate_power_w"],
                        "W",
                        lower_is_better=True,
                    ),
                ),
                (
                    f"FPS/W @ {float(reference_temp_c):.1f}C",
                    format_user_value(initial_norm_fps_w, "FPS/W", precision=5),
                    format_user_value(
                        normalized["previous_fps_per_w"], "FPS/W", precision=5
                    ),
                    format_user_value(
                        normalized["candidate_fps_per_w"], "FPS/W", precision=5
                    ),
                    format_user_change(
                        normalized["previous_fps_per_w"],
                        normalized["candidate_fps_per_w"],
                        "FPS/W",
                        precision=5,
                    ),
                    _change_vs_initial(
                        initial_norm_fps_w,
                        normalized["candidate_fps_per_w"],
                        "FPS/W",
                        precision=5,
                    ),
                ),
            ]
    log_user_table(
        log,
        title="This Step Compared With Initial And Previous Stable",
        headers=(
            "Metric",
            "Initial",
            "Previous stable",
            "This step",
            "Change vs previous",
            "Change vs initial",
        ),
        rows=[
            (
                "Voltage",
                _voltage(initial_probe),
                _voltage(previous_probe),
                f"{int(candidate_probe.candidate_voltage_mv)}mV",
                _signed_int_change(
                    previous_probe.candidate_voltage_mv
                    if previous_probe is not None
                    else None,
                    candidate_probe.candidate_voltage_mv,
                    "mV",
                ),
                _signed_int_change(
                    initial_probe.candidate_voltage_mv
                    if initial_probe is not None
                    else None,
                    candidate_probe.candidate_voltage_mv,
                    "mV",
                ),
            ),
            (
                "Measured voltage",
                format_user_value(
                    initial_probe.avg_voltage_mv if initial_probe is not None else None,
                    "mV",
                ),
                format_user_value(
                    previous_probe.avg_voltage_mv
                    if previous_probe is not None
                    else None,
                    "mV",
                ),
                format_user_value(candidate_probe.avg_voltage_mv, "mV"),
                format_user_change(
                    previous_probe.avg_voltage_mv
                    if previous_probe is not None
                    else None,
                    candidate_probe.avg_voltage_mv,
                    "mV",
                    lower_is_better=True,
                ),
                _change_vs_initial(
                    initial_probe.avg_voltage_mv if initial_probe is not None else None,
                    candidate_probe.avg_voltage_mv,
                    "mV",
                    lower_is_better=True,
                ),
            ),
            (
                "Live voltage after",
                _live_voltage_after(initial_probe),
                _live_voltage_after(previous_probe),
                _live_voltage_after(candidate_probe),
                _signed_int_change(
                    previous_probe.live_voltage_after_mv
                    if previous_probe is not None
                    else None,
                    candidate_probe.live_voltage_after_mv,
                    "mV",
                ),
                _signed_int_change(
                    initial_probe.live_voltage_after_mv
                    if initial_probe is not None
                    else None,
                    candidate_probe.live_voltage_after_mv,
                    "mV",
                ),
            ),
            (
                "Target clock",
                _initial_clock(),
                _target_clock(previous_probe),
                f"{int(candidate_probe.lock_clock_mhz)}MHz",
                _signed_int_change(
                    previous_probe.lock_clock_mhz
                    if previous_probe is not None
                    else None,
                    candidate_probe.lock_clock_mhz,
                    "MHz",
                ),
                _signed_int_change(
                    _initial_measured_clock_mhz(),
                    candidate_probe.lock_clock_mhz,
                    "MHz",
                ),
            ),
            (
                "Power draw",
                format_user_value(
                    initial_probe.avg_power_w if initial_probe is not None else None,
                    "W",
                ),
                format_user_value(
                    previous_probe.avg_power_w if previous_probe is not None else None,
                    "W",
                ),
                format_user_value(candidate_probe.avg_power_w, "W"),
                format_user_change(
                    previous_probe.avg_power_w if previous_probe is not None else None,
                    candidate_probe.avg_power_w,
                    "W",
                    lower_is_better=True,
                ),
                _change_vs_initial(
                    initial_probe.avg_power_w if initial_probe is not None else None,
                    candidate_probe.avg_power_w,
                    "W",
                    lower_is_better=True,
                ),
            ),
            (
                "FPS per watt",
                _fps_per_w(initial_probe),
                _fps_per_w(previous_probe),
                _fps_per_w(candidate_probe),
                format_user_change(
                    previous_probe.efficiency_fps_per_w
                    if previous_probe is not None
                    else None,
                    candidate_probe.efficiency_fps_per_w,
                    "FPS/W",
                    precision=5,
                ),
                _change_vs_initial(
                    initial_probe.efficiency_fps_per_w
                    if initial_probe is not None
                    else None,
                    candidate_probe.efficiency_fps_per_w,
                    "FPS/W",
                    precision=5,
                ),
            ),
            *temperature_normalized_rows,
        ],
    )


def log_user_readable_final_summary(
    log: Callable[[str], None],
    *,
    baseline_probe: AutoUvProbeSummary | None,
    final_probe: AutoUvProbeSummary | None,
    final_voltage_mv: int,
    final_lock_clock_mhz: int,
    clock_drop_margin_pct: float | None,
    curve_path: Path,
    result_title: str = "Auto-UV Result Summary",
    final_curve_label: str = "Final curve",
    final_verification_status: str | None = None,
    final_curve_overclock: dict | None = None,
) -> None:
    if baseline_probe is None or final_probe is None:
        log("")
        log(result_title)
        log("Result data was incomplete, but the final curve was saved.")
        log(f"Voltage/Frequency curve: {curve_path}")
        return

    baseline_clock = baseline_probe.avg_core_clock_mhz
    final_clock = final_probe.avg_core_clock_mhz
    baseline_power = baseline_probe.avg_power_w
    final_power = final_probe.avg_power_w
    baseline_fps_w = baseline_probe.efficiency_fps_per_w
    final_fps_w = final_probe.efficiency_fps_per_w
    baseline_mhz_w = baseline_probe.efficiency_mhz_per_w
    final_mhz_w = final_probe.efficiency_mhz_per_w

    clock_drop_pct = (
        ((float(baseline_clock) - float(final_clock)) / float(baseline_clock)) * 100.0
        if baseline_clock not in (None, 0.0) and final_clock is not None
        else None
    )
    power_saved_w = (
        float(baseline_power) - float(final_power)
        if baseline_power is not None and final_power is not None
        else None
    )
    power_saved_pct = (
        (float(power_saved_w) / float(baseline_power)) * 100.0
        if power_saved_w is not None and baseline_power not in (None, 0.0)
        else None
    )
    within_margin = (
        clock_drop_margin_pct is not None
        and clock_drop_pct is not None
        and float(clock_drop_pct) <= float(clock_drop_margin_pct)
    )

    log("")
    log(result_title)
    if power_saved_w is not None and power_saved_pct is not None:
        log(
            "Plain English: the GPU used "
            f"{power_saved_w:.1f}W less power ({power_saved_pct:.1f}% lower) "
            f"at the final tested curve."
        )
    if clock_drop_margin_pct is not None and clock_drop_pct is not None:
        log(
            "Performance guardrail: core clock drop was "
            f"{clock_drop_pct:.2f}% with an allowed limit of "
            f"{float(clock_drop_margin_pct):.1f}%: "
            f"{'PASS' if within_margin else 'CHECK'}."
        )
    log(
        f"{final_curve_label}: {int(final_lock_clock_mhz)}MHz at {int(final_voltage_mv)}mV"
    )
    if final_verification_status:
        log(f"Final verification: {final_verification_status}")
    if final_curve_overclock is not None:
        lock_offset = final_curve_overclock.get("lock_offset_mhz")
        lock_vanilla = final_curve_overclock.get("lock_vanilla_mhz")
        lock_final = final_curve_overclock.get("lock_final_mhz")
        lock_voltage = final_curve_overclock.get("lock_voltage_mv")
        min_offset = final_curve_overclock.get("min_offset_mhz")
        max_offset = final_curve_overclock.get("max_offset_mhz")
        avg_offset = final_curve_overclock.get("avg_offset_mhz")
        positive_points = final_curve_overclock.get("positive_points")
        total_points = final_curve_overclock.get("total_points")
        if (
            lock_offset is not None
            and lock_voltage is not None
            and lock_vanilla is not None
            and lock_final is not None
        ):
            sign = "+" if float(lock_offset) > 0.0 else ""
            log(
                "Curve overclock vs vanilla: "
                f"{sign}{float(lock_offset):.0f}MHz at {int(lock_voltage)}mV "
                f"(vanilla {float(lock_vanilla):.0f}MHz -> final {float(lock_final):.0f}MHz)."
            )
        if (
            min_offset is not None
            and max_offset is not None
            and avg_offset is not None
            and positive_points is not None
            and total_points is not None
        ):
            log(
                "Across editable bins: "
                f"offset range {float(min_offset):.0f}.."
                f"{float(max_offset):.0f}MHz, "
                f"average {float(avg_offset):.1f}MHz; "
                f"{int(positive_points)}/"
                f"{int(total_points)} bins are above vanilla."
            )

    log_user_table(
        log,
        title="Before vs After",
        headers=("Metric", "Before", "After", "Change"),
        rows=[
            (
                "Power draw",
                format_user_value(baseline_power, "W"),
                format_user_value(final_power, "W"),
                format_user_change(
                    baseline_power, final_power, "W", lower_is_better=True
                ),
            ),
            (
                "Core clock",
                format_user_value(baseline_clock, "MHz"),
                format_user_value(final_clock, "MHz"),
                format_user_change(baseline_clock, final_clock, "MHz"),
            ),
            (
                "FPS per watt",
                format_user_value(baseline_fps_w, "FPS/W", precision=5),
                format_user_value(final_fps_w, "FPS/W", precision=5),
                format_user_change(baseline_fps_w, final_fps_w, "FPS/W", precision=5),
            ),
            (
                "MHz per watt",
                format_user_value(baseline_mhz_w, "MHz/W", precision=5),
                format_user_value(final_mhz_w, "MHz/W", precision=5),
                format_user_change(baseline_mhz_w, final_mhz_w, "MHz/W", precision=5),
            ),
        ],
    )

    log_user_table(
        log,
        title="Saved Files",
        headers=("File", "Purpose"),
        rows=[
            (str(curve_path), "Final voltage/frequency curve used by the daemon"),
        ],
    )
