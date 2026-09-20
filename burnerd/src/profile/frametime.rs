//! Statistics and slowdown evidence shared by frame telemetry sources.

use super::round_half_even;

/// `_p95_us(values)` — drop `<= 0`, sort, index `round((n-1)*0.95)` (banker's).
fn quantile_index(n: usize, q: f64) -> usize {
    let raw = round_half_even((n as f64 - 1.0) * q) as i64;
    raw.clamp(0, n as i64 - 1) as usize
}

pub(super) fn p95_us(values: &[i64]) -> Option<i64> {
    let mut v: Vec<i64> = values.iter().copied().filter(|&x| x > 0).collect();
    if v.is_empty() {
        return None;
    }
    v.sort_unstable();
    let n = v.len();
    Some(v[quantile_index(n, 0.95)])
}

/// Every statistic the window offers, from one filtered copy and one sort.
///
/// The percentiles are order statistics of the same sample, so they share the
/// work instead of taking a copy and a sort each; the miss count needs no
/// ordering at all and rides along in the filtering pass.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct FrametimeStats {
    pub p95_us: i64,
    /// Median of the same accepted set as `p95_us`.
    pub p50_us: i64,
    /// Share of frames over the deadline, when one was supplied.
    ///
    /// The real-time literature calls this the deadline miss ratio: not "how
    /// late was the worst frame" but "how much of the window blew its budget".
    /// A tail can be one stutter; a ratio cannot.
    pub miss_ratio: Option<f64>,
}

pub(super) fn frametime_stats(
    values: &[i64],
    miss_deadline_us: Option<i64>,
) -> Option<FrametimeStats> {
    let mut v: Vec<i64> = Vec::with_capacity(values.len());
    let mut missed = 0usize;
    for &value in values {
        if value <= 0 {
            continue;
        }
        if miss_deadline_us.is_some_and(|deadline| value > deadline) {
            missed += 1;
        }
        v.push(value);
    }
    if v.is_empty() {
        return None;
    }
    v.sort_unstable();
    let n = v.len();
    Some(FrametimeStats {
        p95_us: v[quantile_index(n, 0.95)],
        p50_us: v[quantile_index(n, 0.5)],
        miss_ratio: miss_deadline_us.map(|_| missed as f64 / n as f64),
    })
}

impl FrametimeStats {
    /// A slow tail alone does not justify an immediate jump to Performance.
    /// Callers must supply base-frame statistics from one accepted window;
    /// generated output cannot be compared with a base-frame FPS target.
    pub fn supports_immediate_promotion(self, target_ms: f64, min_miss_ratio: f64) -> bool {
        self.p50_us as f64 / 1000.0 > target_ms
            && self.miss_ratio.is_none_or(|ratio| ratio >= min_miss_ratio)
    }
}
