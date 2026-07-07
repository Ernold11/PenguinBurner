//! Journal line formatting — a faithful port of `common/log_format.py` and
//! `common/runtime_log_lines.py`. These strings are machine-read by the Python
//! log parsers/tests, so the grammar (tag column width 6, `" | "` joins,
//! dropped-empty sections, bucketed status signature) is load-bearing.

use crate::gpu::round_half_even;

const TAG_WIDTH: usize = 6;

/// `single_line_text`: flatten embedded newlines into `" | "`-joined non-empty
/// stripped parts (Python `str.splitlines()` + strip).
pub fn single_line_text(value: &str) -> String {
    value
        .split('\n')
        .map(|part| part.trim_end_matches('\r').trim())
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>()
        .join(" | ")
}

/// `format_log_line(tag, *sections)`: drop None/empty sections, join with
/// `" | "`, prefix the tag left-padded to width 6.
pub fn format_log_line(tag: &str, sections: &[Option<String>]) -> String {
    let body = sections
        .iter()
        .filter_map(|section| section.as_deref())
        .map(single_line_text)
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join(" | ");
    let tag = tag.trim();
    if tag.is_empty() {
        return body;
    }
    if body.is_empty() {
        return tag.to_string();
    }
    format!("{tag:<TAG_WIDTH$} | {body}")
}

fn int_round(value: f64) -> i64 {
    round_half_even(value) as i64
}

/// `format_clock_voltage`: `"2642MHz @ 860mV"` (or one side, or empty).
pub fn format_clock_voltage(core_clock_mhz: Option<f64>, voltage_mv: Option<f64>) -> String {
    let clock = core_clock_mhz.map(|c| format!("{}MHz", int_round(c)));
    let volt = voltage_mv.map(|v| format!("{}mV", int_round(v)));
    match (clock, volt) {
        (Some(c), Some(v)) => format!("{c} @ {v}"),
        (Some(c), None) => c,
        (None, Some(v)) => v,
        (None, None) => String::new(),
    }
}

/// `format_fan_section`: `"fan 35% manual"` / `"fan auto"` / `"fan off"`.
pub fn format_fan_section(fan_pct: Option<f64>, fan_mode: &str) -> String {
    let mode = fan_mode.trim();
    if mode == "disabled" {
        return "fan off".to_string();
    }
    match fan_pct {
        // `format!("fan {mode}")` always starts with "fan", so the trim is never
        // empty (empty mode → "fan"); no empty-string fallback branch needed.
        None => format!("fan {mode}").trim().to_string(),
        Some(pct) => format!("fan {}% {}", int_round(pct), mode)
            .trim()
            .to_string(),
    }
}

#[allow(clippy::too_many_arguments)]
pub fn status_line(
    temp_c: Option<f64>,
    power_w: Option<f64>,
    core_clock_mhz: Option<f64>,
    voltage_mv: Option<f64>,
    fan_pct: Option<f64>,
    fan_mode: &str,
    tier: Option<&str>,
) -> String {
    format_log_line(
        "status",
        &[
            temp_c.map(|t| format!("{t:.0}C")),
            power_w.map(|p| format!("{p:.0}W")),
            Some(format_clock_voltage(core_clock_mhz, voltage_mv)),
            Some(format_fan_section(fan_pct, fan_mode)),
            tier.map(str::to_string),
        ],
    )
}

/// Coarse status signature: continuous fields bucketed, fan/mode/tier exact.
#[derive(Debug, Clone, PartialEq)]
pub struct StatusSignature {
    temp: Option<i64>,
    power: Option<i64>,
    clock: Option<i64>,
    voltage: Option<i64>,
    fan_pct: Option<i64>,
    fan_mode: String,
    tier: Option<String>,
}

fn bucket(value: Option<f64>, size: f64) -> Option<i64> {
    value.map(|v| round_half_even(v / size) as i64)
}

#[allow(clippy::too_many_arguments)]
pub fn status_signature(
    temp_c: Option<f64>,
    power_w: Option<f64>,
    core_clock_mhz: Option<f64>,
    voltage_mv: Option<f64>,
    fan_pct: Option<f64>,
    fan_mode: &str,
    tier: Option<&str>,
) -> StatusSignature {
    StatusSignature {
        temp: bucket(temp_c, 2.0),
        power: bucket(power_w, 10.0),
        clock: bucket(core_clock_mhz, 30.0),
        voltage: bucket(voltage_mv, 5.0),
        fan_pct: fan_pct.map(int_round),
        fan_mode: fan_mode.to_string(),
        tier: tier.map(str::to_string),
    }
}

pub fn tier_switch_line(from_tier: &str, to_tier: &str, reason: &str) -> String {
    format_log_line(
        "tier",
        &[
            Some(format!("{from_tier} → {to_tier}")),
            Some(reason.to_string()),
            Some(String::new()),
        ],
    )
}

pub fn fan_event_line(event: &str, temp_c: Option<f64>) -> String {
    format_log_line(
        "fan",
        &[
            Some(event.to_string()),
            temp_c.map(|t| format!("{t:.0}C")),
            None,
        ],
    )
}

pub fn emergency_line(event: &str, temp_c: Option<f64>, fan_mode: &str) -> String {
    format_log_line(
        "emerg",
        &[
            Some(event.to_string()),
            temp_c.map(|t| format!("{t:.0}C")),
            Some(format_fan_section(None, fan_mode)),
        ],
    )
}

pub fn warn_line(what: &str, detail: &str) -> String {
    format_log_line("warn", &[Some(what.to_string()), Some(detail.to_string())])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn log_line_grammar() {
        assert_eq!(
            format_log_line(
                "status",
                &[
                    Some("58C".into()),
                    Some("257W".into()),
                    Some("fan 35% manual".into()),
                    Some("Balanced".into())
                ]
            ),
            "status | 58C | 257W | fan 35% manual | Balanced"
        );
        assert_eq!(
            format_log_line(
                "warn",
                &[
                    Some("overlay publish unavailable".into()),
                    None,
                    Some("".into())
                ]
            ),
            "warn   | overlay publish unavailable"
        );
    }

    #[test]
    fn status_line_shapes() {
        assert_eq!(
            status_line(
                Some(58.0),
                Some(257.0),
                Some(2642.0),
                Some(860.0),
                Some(35.0),
                "manual",
                Some("Balanced")
            ),
            "status | 58C | 257W | 2642MHz @ 860mV | fan 35% manual | Balanced"
        );
        // Fan control disabled → "fan off", empty tier dropped.
        assert_eq!(
            status_line(
                Some(40.0),
                None,
                Some(210.0),
                None,
                None,
                "disabled",
                Some("")
            ),
            "status | 40C | 210MHz | fan off"
        );
    }

    #[test]
    fn signature_buckets() {
        let a = status_signature(
            Some(58.0),
            Some(257.0),
            Some(2642.0),
            Some(860.0),
            Some(35.0),
            "manual",
            Some("Balanced"),
        );
        // Within the same buckets (temp/2, power/10, clock/30, volt/5) → equal.
        let b = status_signature(
            Some(58.9),
            Some(258.0),
            Some(2650.0),
            Some(861.0),
            Some(35.0),
            "manual",
            Some("Balanced"),
        );
        assert_eq!(a, b);
        // Fan pct differs exactly → not equal.
        let c = status_signature(
            Some(58.0),
            Some(257.0),
            Some(2642.0),
            Some(860.0),
            Some(36.0),
            "manual",
            Some("Balanced"),
        );
        assert_ne!(a, c);
    }

    #[test]
    fn single_line_flattens() {
        assert_eq!(single_line_text("a\n\n  b  \nc"), "a | b | c");
    }
}
