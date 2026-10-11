"""Scan safeguards and the explicit opt-in to retry climbs after a freeze."""

from __future__ import annotations

from dataclasses import dataclass, replace

from auto_uv.run.edge_margin import EdgeMargin

# Voltage bins (about 6 mV each) Performance may add above the Balanced-proven
# voltage to climb past rungs the blacklist or the edge margin close at that
# voltage. On the issue 109 card Balanced lands at 912 to 918 mV and the
# 1920 MHz crash band closes everything at or below 937, so the climb needs
# 943: six bins from 912. The tester's hand-tuned Performance sits 13 to
# 38 mV above his Balanced point, the same range.
PERFORMANCE_VOLTAGE_HEADROOM_BINS = 6


@dataclass(frozen=True, slots=True)
class TuningPolicy:
    edge_margin: EdgeMargin
    # Whether a climb may retry, at a higher voltage, a rung (or anything
    # above it) that already froze the host at a Performance-class voltage.
    # Retrying is opt-in; it does not relax any of the other safeguards.
    retry_frozen_rungs: bool

    @classmethod
    def build(
        cls, runtime_options: dict, unsafe_entries: list[dict], base_curve: list[dict]
    ) -> TuningPolicy:
        return cls(
            edge_margin=EdgeMargin(unsafe_entries, base_curve),
            retry_frozen_rungs=runtime_options.get("auto_uv_retry_after_freeze") is True,
        )

    def with_entries(self, unsafe_entries: list[dict]) -> TuningPolicy:
        """Bind the edge margin to the list a scan accumulates freezes into."""
        return replace(
            self,
            edge_margin=EdgeMargin(
                unsafe_entries, self.edge_margin.base_curve,
                margin_bins=self.edge_margin.margin_bins,
            ),
        )

    def describe(self) -> str:
        state = "on" if self.retry_frozen_rungs else "off"
        margin = self.edge_margin.describe() or "no freezes on record"
        return (
            f"retry after freeze: {state}; {margin}; "
            f"performance may add {PERFORMANCE_VOLTAGE_HEADROOM_BINS} voltage bins"
        )
