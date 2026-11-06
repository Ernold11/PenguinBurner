from __future__ import annotations

import pytest
from auto_uv_test_data import rtx_3080_issue109_stock_curve_warm

from auto_uv.run.tuning_policy import TuningPolicy

FREEZE = {"candidate_voltage_mv": 937, "lock_clock_mhz": 1920, "reason": "previous-run-abruptly-ended"}


@pytest.mark.parametrize("enabled", [False, True])
def test_retry_after_freeze_keeps_all_other_guards(enabled):
    curve = rtx_3080_issue109_stock_curve_warm()
    policy = TuningPolicy.build({"auto_uv_retry_after_freeze": enabled}, [FREEZE], curve)
    assert policy.retry_frozen_rungs is enabled
    assert f"retry after freeze: {'on' if enabled else 'off'}" in policy.describe()
    assert policy.edge_margin.block_reason(candidate_voltage_mv=937, lock_clock_mhz=1920)


@pytest.mark.parametrize("value", [None, False, "false", "true", 0, 1])
def test_freeze_retry_needs_an_explicit_true(value):
    curve = rtx_3080_issue109_stock_curve_warm()
    assert not TuningPolicy.build({}, [], curve).retry_frozen_rungs
    assert not TuningPolicy.build({"auto_uv_retry_after_freeze": value}, [], curve).retry_frozen_rungs


def test_with_entries_rebinds_the_margin_to_the_live_list():
    curve = rtx_3080_issue109_stock_curve_warm()
    policy = TuningPolicy.build({}, [], curve)
    assert policy.edge_margin.points() == []
    live: list[dict] = []
    bound = policy.with_entries(live)
    live.append(FREEZE)
    assert len(bound.edge_margin.points()) == 1
    assert bound.edge_margin.margin_bins == policy.edge_margin.margin_bins
