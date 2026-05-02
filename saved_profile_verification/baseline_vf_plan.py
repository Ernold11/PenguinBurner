"""Build the baseline V/F plan used to compare user-edited profiles.

The baseline keeps the same voltage bins and restores each point to its base clock.
"""

from __future__ import annotations

from penguin_burner_errors import NvmlError


PROFILE_VERIFY_BASELINE_DURATION_S = 60
PROFILE_VERIFY_BASELINE_MIN_DURATION_S = 10


def profile_verification_baseline_duration_s(duration_s: int) -> int:
    return max(
        int(PROFILE_VERIFY_BASELINE_MIN_DURATION_S),
        min(
            int(PROFILE_VERIFY_BASELINE_DURATION_S),
            max(1, int(duration_s)) // 10,
        ),
    )


def base_vf_plan_from_profile_plan(plan: list[dict]) -> list[dict]:
    base_plan = []
    for raw in list(plan or []):
        item = dict(raw)
        try:
            base_mhz = int(round(float(item["base_mhz"])))
            item["target_mhz"] = int(base_mhz)
            item["new_offset_mhz"] = 0
        except (KeyError, TypeError, ValueError) as exc:
            raise NvmlError(f"profile baseline V/F point is invalid: {raw}") from exc
        base_plan.append(item)
    if not base_plan:
        raise NvmlError("profile baseline V/F plan is empty")
    return base_plan
