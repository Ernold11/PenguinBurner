from __future__ import annotations

from .models import AutoUvCurveCandidate, AutoUvError, VoltageCurve
from .tuning import AUTO_UV_CURVE_TUNING, AUTO_UV_VOLTAGE_PHASE_TUNING
from .unsafe_classification import _unsafe_entry_blocks_future_search


def _percent(value: float | int) -> float:
    return max(0.0, float(value) / 100.0)


def _nearest_voltage_bin(plan: list[dict], voltage_mv: int) -> int:
    curve = VoltageCurve.from_plan(plan)
    available = curve.editable_voltage_bins
    try:
        if not available:
            raise ValueError
        return int(min(available, key=lambda value: abs(int(value) - int(voltage_mv))))
    except ValueError:
        raise AutoUvError(
            "the translated Linux V/F plan did not contain any voltage bins"
        )


def _lower_voltage_bins(
    plan: list[dict],
    start_voltage_mv: int,
    *,
    preserve_base_below_mv: int | None,
    min_search_voltage_mv: int | None = None,
) -> list[int]:
    return VoltageCurve.from_plan(plan).lower_voltage_bins(
        start_voltage_mv,
        preserve_base_below_mv=preserve_base_below_mv,
        min_search_voltage_mv=min_search_voltage_mv,
    )


def _next_higher_voltage_bin(plan: list[dict], voltage_mv: int) -> int | None:
    return VoltageCurve.from_plan(plan).next_higher_voltage_bin(voltage_mv)


def _unsafe_min_search_voltage_mv(
    *,
    plan: list[dict],
    start_voltage_mv: int,
    unsafe_entries: list[dict],
) -> tuple[int | None, int | None]:
    unsafe_floor_mv = None
    for entry in unsafe_entries:
        if not _unsafe_entry_blocks_future_search(entry):
            continue
        try:
            voltage_mv = int(entry["candidate_voltage_mv"])
        except (KeyError, TypeError, ValueError):
            continue
        try:
            lock_clock_mhz = int(entry.get("lock_clock_mhz", 0) or 0)
        except (TypeError, ValueError):
            lock_clock_mhz = 0
        if lock_clock_mhz > 0 or isinstance(entry.get("blocked_lock_clock_mhz"), list):
            continue
        if int(voltage_mv) >= int(start_voltage_mv):
            continue
        if unsafe_floor_mv is None or int(voltage_mv) > int(unsafe_floor_mv):
            unsafe_floor_mv = int(voltage_mv)
    if unsafe_floor_mv is None:
        return None, None
    return unsafe_floor_mv, _next_higher_voltage_bin(plan, int(unsafe_floor_mv))


def _unsafe_entry_blocks_candidate(
    entry: dict,
    *,
    candidate_voltage_mv: int,
    lock_clock_mhz: int,
) -> bool:
    if not _unsafe_entry_blocks_future_search(entry):
        return False
    try:
        unsafe_voltage_mv = int(entry["candidate_voltage_mv"])
    except (KeyError, TypeError, ValueError):
        return False
    try:
        unsafe_lock_clock_mhz = int(entry["lock_clock_mhz"])
    except (KeyError, TypeError, ValueError):
        unsafe_lock_clock_mhz = 0

    blocked_lock_floor_mhz = _unsafe_entry_clock_floor_mhz(
        entry,
        fallback_lock_clock_mhz=int(unsafe_lock_clock_mhz),
    )

    # Legacy entries without a clock are voltage-only. Clock-aware entries block
    # the failed voltage and lower voltages only at the recorded failed-clock
    # band, so a much lower target at the same voltage remains testable.
    if unsafe_lock_clock_mhz <= 0:
        return int(candidate_voltage_mv) <= int(unsafe_voltage_mv)
    return int(candidate_voltage_mv) <= int(unsafe_voltage_mv) and int(
        lock_clock_mhz
    ) >= int(blocked_lock_floor_mhz)


def _unsafe_entry_clock_floor_mhz(
    entry: dict,
    *,
    fallback_lock_clock_mhz: int,
) -> int:
    clocks = []
    blocked = entry.get("blocked_lock_clock_mhz")
    if isinstance(blocked, list):
        for value in blocked:
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                clocks.append(int(parsed))
    if clocks:
        return min(clocks)
    return max(0, int(fallback_lock_clock_mhz))


def _unsafe_candidate_block_reason(
    unsafe_entries: list[dict],
    *,
    candidate_voltage_mv: int,
    lock_clock_mhz: int,
) -> str:
    for entry in unsafe_entries:
        if not _unsafe_entry_blocks_candidate(
            entry,
            candidate_voltage_mv=int(candidate_voltage_mv),
            lock_clock_mhz=int(lock_clock_mhz),
        ):
            continue
        try:
            unsafe_voltage_mv = int(entry["candidate_voltage_mv"])
        except (KeyError, TypeError, ValueError):
            unsafe_voltage_mv = int(candidate_voltage_mv)
        try:
            unsafe_clock_text = f"{int(entry['lock_clock_mhz'])}MHz"
            fallback_clock_mhz = int(entry["lock_clock_mhz"])
        except (KeyError, TypeError, ValueError):
            unsafe_clock_text = "unknown"
            fallback_clock_mhz = 0
        clock_floor_mhz = _unsafe_entry_clock_floor_mhz(
            entry,
            fallback_lock_clock_mhz=int(fallback_clock_mhz),
        )
        band_text = (
            f" band>= {int(clock_floor_mhz)}MHz"
            if int(clock_floor_mhz) > 0 and unsafe_clock_text != "unknown"
            else ""
        )
        return (
            "cached unsafe point "
            f"{int(unsafe_voltage_mv)}mV@{unsafe_clock_text}{band_text}"
        )
    return ""


def _higher_voltage_bins(plan: list[dict], voltage_mv: int) -> list[int]:
    return VoltageCurve.from_plan(plan).higher_voltage_bins(voltage_mv)


def _validate_auto_uv_source_plan(plan: list[dict]) -> None:
    curve = VoltageCurve.from_plan(plan)
    editable_points = curve.editable_points
    editable_voltage_bins = curve.editable_voltage_bins
    if len(editable_points) < 3:
        raise AutoUvError(
            "auto-UV needs at least 3 editable voltage-based core V/F points; "
            f"driver exposed {len(editable_points)}"
        )
    if len(set(editable_voltage_bins)) != len(editable_voltage_bins):
        raise AutoUvError("auto-UV V/F table contains duplicate editable voltage bins")
    bad_points = [
        point
        for point in editable_points
        if int(point.voltage_mv) <= 0
        or int(point.base_mhz) <= 0
        or int(point.target_mhz) <= 0
    ]
    if bad_points:
        first = bad_points[0]
        raise AutoUvError(
            "auto-UV V/F table contains invalid editable point "
            f"index={int(first.index)} voltage={int(first.voltage_mv)}mV "
            f"base={int(first.base_mhz)}MHz target={int(first.target_mhz)}MHz"
        )


def _select_aggressive_voltage_bins(
    bins_descending: list[int],
    *,
    start_voltage_mv: int,
) -> list[int]:
    selected: list[int] = []
    cursor = 0
    while cursor < len(bins_descending):
        ratio = (
            float(bins_descending[cursor]) / float(start_voltage_mv)
            if int(start_voltage_mv) > 0
            else 1.0
        )
        if ratio >= _percent(AUTO_UV_VOLTAGE_PHASE_TUNING.medium_voltage_pct):
            step_bins = min(
                AUTO_UV_CURVE_TUNING.aggressive_max_step_bins,
                max(
                    AUTO_UV_CURVE_TUNING.aggressive_min_step_bins,
                    len(bins_descending) - cursor,
                ),
            )
        else:
            step_bins = AUTO_UV_CURVE_TUNING.fine_step_bins
        pick_index = min(cursor + step_bins - 1, len(bins_descending) - 1)
        voltage_mv = int(bins_descending[pick_index])
        selected.append(int(voltage_mv))
        cursor = int(pick_index) + 1
    if bins_descending:
        final_candidate_mv = int(bins_descending[-1])
        if final_candidate_mv not in selected:
            selected.append(final_candidate_mv)
    return selected


def _filter_effective_voltage_candidates(
    bins_descending: list[int],
    *,
    stable_voltage_mv: int,
    reference_actual_voltage_mv: float | None,
) -> list[int]:
    if not bins_descending:
        return []
    if reference_actual_voltage_mv is None:
        return list(bins_descending)

    filtered: list[int] = []
    reference_mv = float(reference_actual_voltage_mv)
    last_kept_mv = int(stable_voltage_mv)
    for voltage_mv in bins_descending:
        ratio = (
            float(voltage_mv) / float(stable_voltage_mv)
            if int(stable_voltage_mv) > 0
            else 1.0
        )
        if ratio >= _percent(AUTO_UV_VOLTAGE_PHASE_TUNING.medium_voltage_pct):
            min_nominal_drop_mv = AUTO_UV_CURVE_TUNING.upper_voltage_nominal_drop_mv
            min_effective_gap_mv = AUTO_UV_CURVE_TUNING.upper_voltage_effective_gap_mv
        else:
            min_nominal_drop_mv = AUTO_UV_CURVE_TUNING.lower_voltage_nominal_drop_mv
            min_effective_gap_mv = AUTO_UV_CURVE_TUNING.lower_voltage_effective_gap_mv
        effective_gap_mv = abs(float(voltage_mv) - float(reference_mv))
        nominal_drop_mv = int(last_kept_mv) - int(voltage_mv)
        if (
            not filtered
            or nominal_drop_mv >= int(min_nominal_drop_mv)
            or effective_gap_mv >= float(min_effective_gap_mv)
        ):
            filtered.append(int(voltage_mv))
            last_kept_mv = int(voltage_mv)
    final_candidate_mv = int(bins_descending[-1])
    if final_candidate_mv not in filtered:
        filtered.append(final_candidate_mv)
    return filtered


def _next_search_candidate_voltage_mv(
    *,
    plan: list[dict],
    start_voltage_mv: int,
    stable_voltage_mv: int,
    reference_actual_voltage_mv: float | None,
    preserve_base_below_mv: int | None,
    min_search_voltage_mv: int | None,
    failed_floor_voltage_mv: int | None = None,
) -> int | None:
    candidate_bins = _lower_voltage_bins(
        plan,
        stable_voltage_mv,
        preserve_base_below_mv=preserve_base_below_mv,
        min_search_voltage_mv=min_search_voltage_mv,
    )
    if failed_floor_voltage_mv is not None:
        candidate_bins = [
            int(voltage_mv)
            for voltage_mv in candidate_bins
            if int(voltage_mv) > int(failed_floor_voltage_mv)
        ]
    if not candidate_bins:
        return None
    candidate_voltage_bins = _select_aggressive_voltage_bins(
        candidate_bins,
        start_voltage_mv=start_voltage_mv,
    )
    candidate_voltage_bins = _filter_effective_voltage_candidates(
        candidate_voltage_bins,
        stable_voltage_mv=stable_voltage_mv,
        reference_actual_voltage_mv=reference_actual_voltage_mv,
    )
    if not candidate_voltage_bins:
        return None
    for candidate_voltage_mv in candidate_voltage_bins:
        if int(candidate_voltage_mv) < int(stable_voltage_mv):
            return int(candidate_voltage_mv)
    return None


def _build_descended_plan(
    source_plan: list[dict],
    *,
    lock_clock_mhz: int,
    candidate_voltage_mv: int,
    below_lock_gap_mhz: int | None = None,
) -> list[dict]:
    curve = VoltageCurve.from_plan(source_plan)
    editable_voltages = curve.editable_voltage_bins
    if int(candidate_voltage_mv) not in editable_voltages:
        raise AutoUvError(
            f"candidate voltage {int(candidate_voltage_mv)}mV is not an editable V/F bin"
        )

    flattened_clock_mhz = int(lock_clock_mhz)
    below_lock_gap = (
        int(below_lock_gap_mhz)
        if below_lock_gap_mhz is not None
        else int(AUTO_UV_CURVE_TUNING.clock_step_mhz)
    )
    below_plateau_cap_mhz = max(
        AUTO_UV_CURVE_TUNING.clock_step_mhz,
        int(flattened_clock_mhz)
        - max(int(AUTO_UV_CURVE_TUNING.clock_step_mhz), int(below_lock_gap)),
    )
    min_voltage_mv = min(int(point.voltage_mv) for point in curve.editable_points)
    ramp_start_voltage_mv = max(
        int(min_voltage_mv),
        int(candidate_voltage_mv) - int(AUTO_UV_CURVE_TUNING.flatten_ramp_window_mv),
    )
    ramp_span_mv = max(1, int(candidate_voltage_mv) - int(ramp_start_voltage_mv))

    plan: list[dict] = []
    for point in curve.points:
        voltage_mv = int(point.voltage_mv)
        base_mhz = int(point.base_mhz)
        original_target_mhz = int(point.target_mhz)
        if point.preserve_base:
            target_mhz = int(base_mhz)
        elif voltage_mv >= int(candidate_voltage_mv):
            target_mhz = int(flattened_clock_mhz)
        elif voltage_mv <= int(ramp_start_voltage_mv):
            target_mhz = int(original_target_mhz)
        else:
            fraction = max(
                0.0,
                min(
                    1.0,
                    (float(voltage_mv) - float(ramp_start_voltage_mv))
                    / float(ramp_span_mv),
                ),
            )
            interpolated_mhz = original_target_mhz + (
                (float(flattened_clock_mhz) - float(original_target_mhz)) * fraction
            )
            target_mhz = min(
                int(flattened_clock_mhz),
                max(
                    int(original_target_mhz),
                    _snap_target_clock(int(round(interpolated_mhz))),
                ),
            )
        if not point.preserve_base and voltage_mv < int(candidate_voltage_mv):
            target_mhz = min(int(target_mhz), int(below_plateau_cap_mhz))
        plan.append(point.with_target_mhz(target_mhz).to_plan_item())
    return plan


def _choose_sustained_clock_target(
    plan: list[dict],
    measured_clock_mhz: float,
) -> int:
    available = sorted({int(item["target_mhz"]) for item in plan})
    if not available:
        raise AutoUvError(
            "the translated Linux V/F plan did not contain any target clocks"
        )

    measured = max(1.0, float(measured_clock_mhz))
    snapped_measured = _snap_target_clock_at_or_below(measured)
    return int(min(max(snapped_measured, min(available)), max(available)))


def _choose_strictly_higher_clock_target(
    plan: list[dict],
    *,
    current_clock_mhz: int,
    desired_clock_mhz: float,
    cap_clock_mhz: float,
) -> int | None:
    current = int(current_clock_mhz)
    cap = int(_choose_sustained_clock_target(plan, float(cap_clock_mhz)))
    if int(cap) <= int(current):
        return None
    desired = max(
        float(desired_clock_mhz),
        float(current) + float(AUTO_UV_CURVE_TUNING.clock_step_mhz),
    )
    target = int(_choose_sustained_clock_target(plan, min(float(cap), desired)))
    if int(target) <= int(current):
        target = int(current) + int(AUTO_UV_CURVE_TUNING.clock_step_mhz)
    while float(target) > float(cap_clock_mhz):
        target -= int(AUTO_UV_CURVE_TUNING.clock_step_mhz)
    return int(min(int(target), int(cap))) if int(target) > int(current) else None


def _find_lock_voltage_for_clock(plan: list[dict], lock_clock_mhz: int) -> int:
    for point in VoltageCurve.from_plan(plan).editable_points:
        if int(point.target_mhz) >= int(lock_clock_mhz):
            return int(point.voltage_mv)
    raise AutoUvError(
        f"the translated Linux V/F plan never reaches {int(lock_clock_mhz)}MHz"
    )


def _build_flatten_target(
    plan: list[dict], *, lock_clock_mhz: int, lock_voltage_mv: int
) -> dict:
    curve = VoltageCurve.from_plan(plan)
    available_voltages = curve.voltage_bins
    end_voltage_mv = (
        available_voltages[-1] if available_voltages else int(lock_voltage_mv)
    )
    tail_point_count = sum(
        1
        for point in curve.points
        if int(point.voltage_mv) >= int(lock_voltage_mv) and not point.preserve_base
    )
    return {
        "source": "measured-sustained-clock",
        "lock_clock_mhz": int(lock_clock_mhz),
        "lock_voltage_mv": int(lock_voltage_mv),
        "end_voltage_mv": int(end_voltage_mv),
        "tail_point_count": int(tail_point_count),
    }


def _snap_target_clock(requested_clock_mhz: int) -> int:
    requested = int(requested_clock_mhz)
    if requested <= 0:
        raise AutoUvError("requested target clock must be positive")
    return int(
        round(float(requested) / float(AUTO_UV_CURVE_TUNING.clock_step_mhz))
        * AUTO_UV_CURVE_TUNING.clock_step_mhz
    )


def _snap_target_clock_at_or_below(requested_clock_mhz: float) -> int:
    requested = float(requested_clock_mhz)
    if requested <= 0.0:
        raise AutoUvError("requested target clock must be positive")
    step = int(AUTO_UV_CURVE_TUNING.clock_step_mhz)
    snapped = int(requested // float(step)) * step
    return max(step, int(snapped))


def _make_curve_candidate(
    source_plan: list[dict],
    *,
    candidate_voltage_mv: int,
    target_clock_mhz: int,
    label: str,
    below_lock_gap_mhz: int | None = None,
) -> AutoUvCurveCandidate:
    effective_below_lock_gap_mhz = (
        int(below_lock_gap_mhz)
        if below_lock_gap_mhz is not None
        else int(AUTO_UV_CURVE_TUNING.clock_step_mhz) * 2
    )
    return AutoUvCurveCandidate(
        label=str(label),
        candidate_voltage_mv=int(candidate_voltage_mv),
        target_clock_mhz=int(target_clock_mhz),
        plan=_build_descended_plan(
            source_plan,
            lock_clock_mhz=target_clock_mhz,
            candidate_voltage_mv=candidate_voltage_mv,
            below_lock_gap_mhz=int(effective_below_lock_gap_mhz),
        ),
    )
