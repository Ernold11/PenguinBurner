"""Monotone pass/fail envelope over the Auto-UV candidate ladder.

The rewrite's core search state. A "ladder" is the ordered list of sweep
candidates: rung 0 is the highest (safest) voltage and each later rung is one
step lower. Stability is assumed monotone along the ladder — when a rung
passes, every rung above it is presumed passable; when a rung hard-fails,
every rung below it is presumed failing. The envelope tracks the deepest pass
and the shallowest hard fail, and the rungs between them are the only ones
still worth probing.

Failure kinds matter and are never conflated:

- Hard instability (crash, Xid, hang, fatal output) moves the fail boundary.
- A low-clock outcome (power wall, clock floor) marks the rung unusable but
  proves nothing about deeper rungs, so it never moves the fail boundary.
  Treating it as instability would wrongly condemn everything below it.

Evidence comes in two tiers. Short probes record a provisional pass; only a
long confirm soak records a confirmed pass. The search settles on geometry
alone, but the edge rung must hold a confirmed pass before it may be trusted.

Safety rule: the next suggested probe never sits more than ``max_jump_rungs``
below the deepest rung that actually passed. Priors may place a guard probe
deeper than the suggestion — that is the scheduler's decision, not ours.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ProbeOutcome(Enum):
    PASS = "pass"
    HARD_FAIL = "hard-fail"
    LOW_CLOCK = "low-clock"


@dataclass(frozen=True)
class RungRecord:
    outcome: ProbeOutcome
    confirmed: bool = False


@dataclass
class LadderEnvelope:
    """Search state over one candidate ladder."""

    rung_count: int
    max_jump_rungs: int = 2
    descend_through_low_clock: bool = False
    records: dict[int, RungRecord] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if int(self.rung_count) <= 0:
            raise ValueError("ladder must have at least one rung")
        if int(self.max_jump_rungs) < 1:
            raise ValueError("max jump must allow at least one rung")

    def record(self, rung: int, outcome: ProbeOutcome, *, confirmed: bool = False) -> None:
        """Store a probe result. A confirmed result may upgrade a provisional
        one; a hard fail always replaces whatever was recorded before it."""
        self._require_valid_rung(rung)
        previous = self.records.get(rung)
        if previous is not None and previous.outcome is ProbeOutcome.HARD_FAIL:
            # A rung that hard-failed once stays failed; a later pass on the
            # same rung is noise (marginal silicon), not exoneration.
            return
        if outcome is ProbeOutcome.HARD_FAIL:
            self.records[rung] = RungRecord(ProbeOutcome.HARD_FAIL)
            return
        if previous is not None and previous.confirmed and not confirmed:
            return
        self.records[rung] = RungRecord(outcome, confirmed=confirmed)

    # ── Boundaries ────────────────────────────────────────────────────────

    def deepest_pass(self) -> int | None:
        passes = [r for r, rec in self.records.items() if rec.outcome is ProbeOutcome.PASS]
        return max(passes) if passes else None

    def shallowest_hard_fail(self) -> int | None:
        fails = [r for r, rec in self.records.items() if rec.outcome is ProbeOutcome.HARD_FAIL]
        return min(fails) if fails else None

    def blocking_low_clock(self) -> int | None:
        """Shallowest low-clock rung, only when the sweep does not descend
        through low-clock bins (non-efficiency semantics)."""
        if self.descend_through_low_clock:
            return None
        lows = [r for r, rec in self.records.items() if rec.outcome is ProbeOutcome.LOW_CLOCK]
        return min(lows) if lows else None

    def search_floor(self) -> int:
        """First rung index that is out of bounds for the search: the
        shallowest hard fail, a blocking low-clock rung, or the ladder end."""
        floor = self.rung_count
        fail = self.shallowest_hard_fail()
        if fail is not None:
            floor = min(floor, fail)
        low = self.blocking_low_clock()
        if low is not None:
            floor = min(floor, low)
        return floor

    # ── Search ────────────────────────────────────────────────────────────

    def next_probe_rung(self) -> int | None:
        """Suggest the next rung to probe, or None when the search is settled.

        The suggestion is the deepest untested rung that is still above the
        search floor and within the jump cap. Rungs already probed (including
        traversable low-clock rungs) count as covered ground, so the cap is
        measured in *untested* rungs below the deepest pass.
        """
        deepest = self.deepest_pass()
        start = 0 if deepest is None else deepest + 1
        untested_allowance = int(self.max_jump_rungs)
        suggestion = None
        for rung in range(start, self.search_floor()):
            if rung in self.records:
                continue
            suggestion = rung
            untested_allowance -= 1
            if untested_allowance <= 0:
                break
        return suggestion

    def is_settled(self) -> bool:
        return self.next_probe_rung() is None

    def edge_rung(self) -> int | None:
        """Deepest passing rung once the search has settled, None before."""
        if not self.is_settled():
            return None
        return self.deepest_pass()

    def edge_needs_confirmation(self) -> bool:
        edge = self.edge_rung()
        if edge is None:
            return False
        return not self.records[edge].confirmed

    def demote_edge(self) -> None:
        """A confirm soak failed on the edge: the provisional pass was wrong.
        Re-record the rung as a hard fail so the boundary moves up and the
        next-shallower pass becomes the edge candidate."""
        edge = self.deepest_pass()
        if edge is None:
            raise ValueError("no passing rung to demote")
        self.records[edge] = RungRecord(ProbeOutcome.HARD_FAIL)

    # ── Persistence ───────────────────────────────────────────────────────

    def to_payload(self) -> dict:
        return {
            "rung_count": int(self.rung_count),
            "max_jump_rungs": int(self.max_jump_rungs),
            "descend_through_low_clock": bool(self.descend_through_low_clock),
            "records": [
                {
                    "rung": rung,
                    "outcome": record.outcome.value,
                    "confirmed": record.confirmed,
                }
                for rung, record in sorted(self.records.items())
            ],
        }

    @classmethod
    def from_payload(cls, payload: dict) -> "LadderEnvelope":
        envelope = cls(
            rung_count=int(payload["rung_count"]),
            max_jump_rungs=int(payload["max_jump_rungs"]),
            descend_through_low_clock=bool(payload["descend_through_low_clock"]),
        )
        for entry in payload.get("records", []):
            envelope.records[int(entry["rung"])] = RungRecord(
                ProbeOutcome(entry["outcome"]),
                confirmed=bool(entry["confirmed"]),
            )
        return envelope

    def _require_valid_rung(self, rung: int) -> None:
        if not 0 <= int(rung) < int(self.rung_count):
            raise ValueError(f"rung {rung} outside ladder of {self.rung_count}")
