"""Apply a saved V/F plan and verify that the driver kept the offsets.

The caller provides the actual apply function so tests and import paths stay simple.
"""

from __future__ import annotations

from penguin_burner_errors import NvmlError
from runtime_gpu_control import (
    detect_vf_curve_reset,
    format_vf_curve_mismatch_preview,
    select_expected_vf_samples,
)


def apply_and_verify_profile_vf_plan(
    vf_curve_reader,
    plan: list[dict],
    *,
    context: str,
    apply_plan_fn,
) -> None:
    apply_plan_fn(vf_curve_reader, plan)
    vf_curve_reader.refresh_points()
    expected_samples = select_expected_vf_samples(plan)
    vf_mismatches = detect_vf_curve_reset(vf_curve_reader, expected_samples)
    if vf_mismatches:
        mismatch_preview = format_vf_curve_mismatch_preview(vf_mismatches)
        raise NvmlError(
            f"Profile verification V/F curve did not match the {context} "
            f"after apply: mismatches={len(vf_mismatches)} "
            f"samples={mismatch_preview}"
        )
