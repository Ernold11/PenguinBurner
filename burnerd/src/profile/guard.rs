//! VF-curve reset guard — port of `runtime/gpu_control/vf_curve_reset_guard.py`.
//! The driver occasionally drops applied per-point offsets; the guard samples the
//! largest expected offsets and reports which voltage bins drifted.

use super::floor_div;
use super::profile_store::PlanItem;
use crate::gpu::VfPoint;

/// The non-zero-offset plan points, sorted by `|new_offset_mhz|` descending,
/// capped at `max_samples` (default 8). Stable sort matches Python's stable
/// `reverse=True` (equal keys keep original order).
pub fn select_expected_vf_samples(plan: &[PlanItem], max_samples: usize) -> Vec<PlanItem> {
    let mut candidates: Vec<PlanItem> = plan
        .iter()
        .copied()
        .filter(|item| item.new_offset_mhz != 0)
        .collect();
    candidates.sort_by_key(|item| std::cmp::Reverse(item.new_offset_mhz.abs()));
    candidates.truncate(max_samples);
    candidates
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct VfMismatch {
    pub index: u32,
    pub expected_offset_mhz: i64,
    pub current_offset_mhz: i64,
    pub voltage_mv: i64,
}

/// Compare each expected sample's offset against the live editable-point offset
/// (`current_offset_khz // 1000`, floored). A mismatch when `|Δ| > tolerance`.
pub fn detect_vf_curve_reset(
    editable_points: &[VfPoint],
    expected_samples: &[PlanItem],
    tolerance_mhz: i64,
) -> Vec<VfMismatch> {
    if expected_samples.is_empty() {
        return Vec::new();
    }
    let mut mismatches = Vec::new();
    for sample in expected_samples {
        let current_offset_mhz = editable_points
            .iter()
            .find(|p| p.index == sample.index)
            .map(|p| floor_div(p.current_offset_khz, 1000))
            .unwrap_or(0);
        if (current_offset_mhz - sample.new_offset_mhz).abs() > tolerance_mhz {
            mismatches.push(VfMismatch {
                index: sample.index,
                expected_offset_mhz: sample.new_offset_mhz,
                current_offset_mhz,
                voltage_mv: sample.voltage_mv,
            });
        }
    }
    mismatches
}

/// `format_vf_curve_mismatch_preview` — `"875mV:+0->+240MHz, ..."`.
pub fn format_vf_curve_mismatch_preview(mismatches: &[VfMismatch], max_items: usize) -> String {
    let mut preview = mismatches
        .iter()
        .take(max_items)
        .map(|m| {
            format!(
                "{}mV:{:+}->{:+}MHz",
                m.voltage_mv, m.current_offset_mhz, m.expected_offset_mhz
            )
        })
        .collect::<Vec<_>>()
        .join(", ");
    if mismatches.len() > max_items {
        preview.push_str(", ...");
    }
    preview
}

#[cfg(test)]
mod tests {
    use super::*;

    fn item(index: u32, voltage_mv: i64, new_offset_mhz: i64) -> PlanItem {
        PlanItem {
            index,
            voltage_mv,
            base_mhz: 0,
            target_mhz: 0,
            new_offset_mhz,
        }
    }

    #[test]
    fn selects_top_8_nonzero_by_abs() {
        let plan: Vec<PlanItem> = (0..12)
            .map(|i| item(i, 700 + i as i64, (i as i64 - 6) * 10))
            .collect();
        let samples = select_expected_vf_samples(&plan, 8);
        assert_eq!(samples.len(), 8);
        // Largest |offset| first: index 0 (offset -60), index 11 (+50)...
        assert_eq!(samples[0].new_offset_mhz.abs(), 60);
        // Zero-offset point (i==6) excluded.
        assert!(samples.iter().all(|s| s.new_offset_mhz != 0));
    }

    #[test]
    fn detects_drift_beyond_tolerance() {
        let expected = vec![item(12, 875, 240)];
        // Live point drifted to +0 (driver reset).
        let live = vec![VfPoint {
            index: 12,
            type_: 0,
            voltage_based: 1,
            current_offset_khz: 0,
            ..VfPoint::default()
        }];
        let mismatches = detect_vf_curve_reset(&live, &expected, 1);
        assert_eq!(mismatches.len(), 1);
        assert_eq!(mismatches[0].current_offset_mhz, 0);
        assert_eq!(mismatches[0].expected_offset_mhz, 240);
        assert_eq!(
            format_vf_curve_mismatch_preview(&mismatches, 4),
            "875mV:+0->+240MHz"
        );
    }

    #[test]
    fn within_tolerance_no_mismatch() {
        let expected = vec![item(12, 875, 240)];
        // 240 MHz == 240500 khz floor→240; diff 0.
        let live = vec![VfPoint {
            index: 12,
            type_: 0,
            voltage_based: 1,
            current_offset_khz: 240_500,
            ..VfPoint::default()
        }];
        assert!(detect_vf_curve_reset(&live, &expected, 1).is_empty());
    }
}
