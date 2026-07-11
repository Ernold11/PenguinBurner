"""Schedule probes for the envelope sweep over a precomputed voltage ladder.

The old loop chose one lower voltage at a time after each stable probe. The
selection functions it used are deterministic over the base curve, so the
whole descent sequence can be computed up front: that sequence is the ladder
the envelope searches. Reusing ``select_aggressive_voltage_bins`` and
``filter_effective_voltage_candidates`` keeps the rung spacing byte-identical
with the old walk.

The scheduler owns three decisions the envelope stays ignorant of:

- Guard placement. A prior from this card's own previous scan may place the
  first probe just above the remembered edge, skipping the boring top of the
  ladder. Table-derived priors never authorize that jump — unknown silicon
  walks down from the top under the envelope's step cap.
- Candidate targets. Each rung's target clock ratchets with the measured
  clock of the deepest passing probe, exactly like the old loop did.
- Confirmation. Once the search settles, the edge rung must survive one long
  confirm soak; a failed confirm demotes the edge and reopens the search.

Every applied result is handed to the journal callback so an interrupted
scan (overnight crash) resumes from the last probe instead of restarting.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from auto_uv.curve.base_vf_curve_voltage_bins import lower_editable_voltage_bins
from auto_uv.envelope import LadderEnvelope, ProbeOutcome
from auto_uv.run.lower_voltage_probe_target import base_curve_target_for_lower_voltage
from auto_uv.run.lower_voltage_search import (
    LowerVoltageSearchRules,
    select_aggressive_voltage_bins,
)


class PriorSource(Enum):
    NONE = "none"
    TABLE = "table"
    OWN_SCAN = "own-scan"


@dataclass(frozen=True, slots=True)
class SweepPrior:
    """Where we expect the edge to be, and how much we trust that guess."""

    source: PriorSource = PriorSource.NONE
    edge_voltage_mv: int | None = None

    def authorizes_guard_jump(self) -> bool:
        # Only this card's own history may skip ladder rungs; a table entry
        # describes the family, not this silicon, which may crash earlier.
        return self.source is PriorSource.OWN_SCAN and self.edge_voltage_mv is not None


@dataclass(frozen=True, slots=True)
class ProbeRequest:
    rung: int
    voltage_mv: int
    target_clock_mhz: int
    confirm: bool = False


def build_voltage_ladder(
    base_curve: list[dict],
    *,
    start_voltage_mv: int,
    min_search_voltage_mv: int | None,
    rules: LowerVoltageSearchRules = LowerVoltageSearchRules(),
) -> list[int]:
    """Precompute the descent sequence the old loop would have walked.

    The old loop called ``select_next_lower_voltage`` once per stable probe
    and took the first pick; iterating that is exactly the aggressive-bin
    sequence below, because the effective-voltage filter never drops the
    first candidate. Selection depends only on the base curve and the scan
    start voltage, so the whole walk is known up front.
    """
    bins = lower_editable_voltage_bins(
        base_curve,
        int(start_voltage_mv),
        min_search_voltage_mv=min_search_voltage_mv,
    )
    if not bins:
        return []
    return select_aggressive_voltage_bins(
        bins,
        start_voltage_mv=int(start_voltage_mv),
        rules=rules,
    )


class SweepScheduler:
    """Drive one ladder search from guard probe to confirmed edge."""

    def __init__(
        self,
        base_curve: list[dict],
        *,
        start_voltage_mv: int,
        stable_target_mhz: int,
        min_search_voltage_mv: int | None = None,
        baseline_core_clock_mhz: float | None = None,
        min_core_clock_pct: float | None = None,
        target_clock_ceiling_mhz: int | None = None,
        prior: SweepPrior = SweepPrior(),
        descend_through_low_clock: bool = False,
        guard_margin_rungs: int = 2,
        max_jump_rungs: int = 2,
        search_rules: LowerVoltageSearchRules = LowerVoltageSearchRules(),
        save_journal: Callable[[dict], None] | None = None,
    ) -> None:
        self._base_curve = base_curve
        self._stable_target_mhz = int(stable_target_mhz)
        self._measured_target_mhz: int | None = None
        self._baseline_core_clock_mhz = baseline_core_clock_mhz
        self._min_core_clock_pct = min_core_clock_pct
        self._target_clock_ceiling_mhz = target_clock_ceiling_mhz
        self._save_journal = save_journal
        self.ladder = build_voltage_ladder(
            base_curve,
            start_voltage_mv=int(start_voltage_mv),
            min_search_voltage_mv=min_search_voltage_mv,
            rules=search_rules,
        )
        self.envelope: LadderEnvelope | None = None
        if self.ladder:
            self.envelope = LadderEnvelope(
                rung_count=len(self.ladder),
                max_jump_rungs=int(max_jump_rungs),
                descend_through_low_clock=bool(descend_through_low_clock),
            )
        self._guard_rung = self._plan_guard_rung(prior, int(guard_margin_rungs))

    # ── Requests ──────────────────────────────────────────────────────────

    def next_request(self) -> ProbeRequest | None:
        """The next probe to run, or None when the sweep is complete."""
        if self.envelope is None:
            return None
        if self._guard_rung is not None and self._guard_rung not in self.envelope.records:
            return self._request_for(self._guard_rung)
        rung = self.envelope.next_probe_rung()
        if rung is not None:
            return self._request_for(rung)
        if self.envelope.edge_needs_confirmation():
            edge = self.envelope.edge_rung()
            assert edge is not None
            return self._request_for(edge, confirm=True)
        return None

    def apply_result(
        self,
        request: ProbeRequest,
        outcome: ProbeOutcome,
        *,
        measured_clock_mhz: int | None = None,
    ) -> None:
        assert self.envelope is not None, "no ladder to record against"
        self.envelope.record(
            request.rung,
            outcome,
            confirmed=bool(request.confirm and outcome is ProbeOutcome.PASS),
        )
        if outcome is ProbeOutcome.PASS and measured_clock_mhz is not None:
            deepest = self.envelope.deepest_pass()
            if deepest == request.rung:
                self._measured_target_mhz = int(measured_clock_mhz)
        if self._save_journal is not None:
            self._save_journal(self.to_payload())

    def is_complete(self) -> bool:
        return self.next_request() is None

    def edge_candidate(self) -> ProbeRequest | None:
        """The confirmed edge as a probe request, once the sweep completed."""
        if self.envelope is None or not self.is_complete():
            return None
        edge = self.envelope.edge_rung()
        if edge is None or self.envelope.edge_needs_confirmation():
            return None
        return self._request_for(edge)

    # ── Persistence (overnight resume) ────────────────────────────────────

    def to_payload(self) -> dict:
        assert self.envelope is not None
        return {
            "ladder": list(self.ladder),
            "envelope": self.envelope.to_payload(),
            "stable_target_mhz": self._stable_target_mhz,
            "measured_target_mhz": self._measured_target_mhz,
            "guard_rung": self._guard_rung,
        }

    def restore_payload(self, payload: dict) -> None:
        """Resume a persisted sweep. The ladder must match: a changed base
        curve or start voltage means the journal describes a different scan."""
        if list(payload.get("ladder", [])) != list(self.ladder):
            raise ValueError("journal ladder does not match this scan's ladder")
        self.envelope = LadderEnvelope.from_payload(payload["envelope"])
        self._stable_target_mhz = int(payload["stable_target_mhz"])
        measured = payload.get("measured_target_mhz")
        self._measured_target_mhz = None if measured is None else int(measured)
        guard = payload.get("guard_rung")
        self._guard_rung = None if guard is None else int(guard)

    # ── Internals ─────────────────────────────────────────────────────────

    def _request_for(self, rung: int, *, confirm: bool = False) -> ProbeRequest:
        voltage_mv = int(self.ladder[rung])
        target_mhz = base_curve_target_for_lower_voltage(
            self._base_curve,
            candidate_voltage_mv=voltage_mv,
            stable_target_mhz=self._stable_target_mhz,
            stable_measured_target_mhz=self._measured_target_mhz,
            baseline_core_clock_mhz=self._baseline_core_clock_mhz,
            min_core_clock_pct=self._min_core_clock_pct,
        )
        if self._target_clock_ceiling_mhz is not None:
            # Bounded scans (validation against known-good profiles) never
            # request a clock above what is already proven on this card.
            target_mhz = min(int(target_mhz), int(self._target_clock_ceiling_mhz))
        return ProbeRequest(
            rung=rung,
            voltage_mv=voltage_mv,
            target_clock_mhz=int(target_mhz),
            confirm=confirm,
        )

    def _plan_guard_rung(self, prior: SweepPrior, guard_margin_rungs: int) -> int | None:
        if not self.ladder or not prior.authorizes_guard_jump():
            return None
        assert prior.edge_voltage_mv is not None
        edge_rung = None
        for rung, voltage_mv in enumerate(self.ladder):
            if int(voltage_mv) <= int(prior.edge_voltage_mv):
                edge_rung = rung
                break
        if edge_rung is None:
            # The remembered edge sits below this ladder; guard near the end.
            edge_rung = len(self.ladder) - 1
        guard = edge_rung - max(0, int(guard_margin_rungs))
        if guard <= 0:
            # No boring top to skip: let the envelope walk normally.
            return None
        return guard
