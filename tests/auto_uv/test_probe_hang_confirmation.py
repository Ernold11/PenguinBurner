from __future__ import annotations

from types import SimpleNamespace

import auto_uv.probes.voltage_probe as probe_module
from auto_uv.probes.voltage_probe import run_probe_with_hang_confirmation
from stability.q2rtx.runtime import FRAME_HANG_WATCHDOG_REASON


def _result(reason: str, *, success: bool) -> SimpleNamespace:
    return SimpleNamespace(reason=reason, success=success)


def _patch_runs(monkeypatch, results: list[SimpleNamespace]) -> list[object]:
    calls: list[object] = []

    def _fake_run(config):
        calls.append(config)
        return results[len(calls) - 1]

    monkeypatch.setattr(probe_module, "run_q2rtx_stability_test", _fake_run)
    return calls


def test_non_hang_result_returns_without_reprobe(monkeypatch) -> None:
    calls = _patch_runs(monkeypatch, [_result("ok", success=True)])

    result = run_probe_with_hang_confirmation(
        SimpleNamespace(), log=lambda *_: None, phase_label="candidate"
    )

    assert result.reason == "ok"
    assert len(calls) == 1  # no confirmation run for a non-hang result


def test_transient_hang_confirmed_stable_keeps_the_voltage(monkeypatch) -> None:
    calls = _patch_runs(
        monkeypatch,
        [
            _result(FRAME_HANG_WATCHDOG_REASON, success=False),
            _result("ok", success=True),
        ],
    )

    result = run_probe_with_hang_confirmation(
        SimpleNamespace(), log=lambda *_: None, phase_label="candidate"
    )

    # The re-probe rendered normally, so the surviving result is a pass -> the
    # voltage is not blacklisted on a one-off hang.
    assert result.success is True
    assert result.reason == "ok"
    assert len(calls) == 2


def test_reproduced_hang_stays_a_hang(monkeypatch) -> None:
    calls = _patch_runs(
        monkeypatch,
        [
            _result(FRAME_HANG_WATCHDOG_REASON, success=False),
            _result(FRAME_HANG_WATCHDOG_REASON, success=False),
        ],
    )

    result = run_probe_with_hang_confirmation(
        SimpleNamespace(), log=lambda *_: None, phase_label="candidate"
    )

    assert result.reason == FRAME_HANG_WATCHDOG_REASON
    assert result.success is False
    assert len(calls) == 2


def test_confirmation_run_starts_with_reset_live_abort_state(monkeypatch) -> None:
    reset_calls: list[int] = []
    calls = _patch_runs(
        monkeypatch,
        [
            _result(FRAME_HANG_WATCHDOG_REASON, success=False),
            _result("ok", success=True),
        ],
    )

    def _reset() -> None:
        # The hung first run must already have happened, and the confirmation
        # run must not have started yet.
        reset_calls.append(len(calls))

    result = run_probe_with_hang_confirmation(
        SimpleNamespace(),
        log=lambda *_: None,
        phase_label="candidate",
        reset_live_abort_state=_reset,
    )

    assert result.success is True
    assert reset_calls == [1]


def test_non_hang_result_does_not_reset_live_abort_state(monkeypatch) -> None:
    reset_calls: list[int] = []
    _patch_runs(monkeypatch, [_result("ok", success=True)])

    result = run_probe_with_hang_confirmation(
        SimpleNamespace(),
        log=lambda *_: None,
        phase_label="candidate",
        reset_live_abort_state=lambda: reset_calls.append(1),
    )

    assert result.reason == "ok"
    assert reset_calls == []


def test_reprobe_with_different_failure_uses_reprobe_result(monkeypatch) -> None:
    calls = _patch_runs(
        monkeypatch,
        [
            _result(FRAME_HANG_WATCHDOG_REASON, success=False),
            _result("nvidia-xid-detected", success=False),
        ],
    )

    result = run_probe_with_hang_confirmation(
        SimpleNamespace(), log=lambda *_: None, phase_label="candidate"
    )

    # A genuine different failure on the re-probe is honoured, not masked.
    assert result.reason == "nvidia-xid-detected"
    assert len(calls) == 2
