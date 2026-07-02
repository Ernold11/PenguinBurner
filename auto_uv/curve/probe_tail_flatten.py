from __future__ import annotations


def flatten_probe_tail(plan: list[dict], *, candidate_voltage_mv: int) -> list[dict]:
    """Cap every editable point above the candidate voltage to the candidate
    clock for the duration of a stability probe.

    A sweep plan may carry a rising tail above the candidate so the saved
    profile can boost further at higher voltages, but that tail is unproven
    while the candidate itself is still under test: a light-load boost
    transient can ride it hundreds of MHz past the locked probe clock and
    hang the GPU. The plan applied during a probe therefore never exceeds
    the clock actually being validated. The candidate's stored plan (and the
    saved profile shape) stay untouched.
    """
    candidate_target_mhz = _candidate_target_mhz(plan, int(candidate_voltage_mv))
    if candidate_target_mhz is None:
        return plan
    guarded = []
    for point in plan:
        if (
            not point.get("preserve_base")
            and int(point["voltage_mv"]) > int(candidate_voltage_mv)
            and int(point["target_mhz"]) > int(candidate_target_mhz)
        ):
            point = dict(point)
            point["target_mhz"] = int(candidate_target_mhz)
            point["new_offset_mhz"] = int(candidate_target_mhz) - int(
                point["base_mhz"]
            )
        guarded.append(point)
    return guarded


def _candidate_target_mhz(plan: list[dict], candidate_voltage_mv: int) -> int | None:
    for point in plan:
        if (
            not point.get("preserve_base")
            and int(point["voltage_mv"]) == int(candidate_voltage_mv)
        ):
            return int(point["target_mhz"])
    return None
