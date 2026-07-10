//! Fan-control math and runtime settings for the immutable RuntimeSpec.

use serde::{Deserialize, Serialize};

pub fn clamp(value: f64, lower: f64, upper: f64) -> f64 {
    lower.max(value.min(upper))
}

/// `validate_curve` — returns the load-bearing error string on failure.
pub fn validate_curve(curve: &[(f64, f64)]) -> Result<(), String> {
    if curve.len() < 2 {
        return Err("curve must contain at least two points".to_string());
    }
    let mut last_temp: Option<f64> = None;
    let mut last_speed: Option<f64> = None;
    for &(temp_c, speed_pct) in curve {
        if let Some(lt) = last_temp {
            if temp_c <= lt {
                return Err("curve temperatures must be strictly increasing".to_string());
            }
        }
        if !(0.0..=100.0).contains(&speed_pct) {
            return Err("curve fan speeds must be in the range 0..100".to_string());
        }
        if let Some(ls) = last_speed {
            if speed_pct < ls {
                return Err("curve fan speeds must not decrease as temperature rises".to_string());
            }
        }
        last_temp = Some(temp_c);
        last_speed = Some(speed_pct);
    }
    Ok(())
}

/// `speed_for_temp` — flat clamp below/above, linear or step interpolation.
pub fn speed_for_temp(temp_c: f64, curve: &[(f64, f64)], mode: &str) -> f64 {
    if temp_c <= curve[0].0 {
        return curve[0].1;
    }
    for index in 1..curve.len() {
        let (left_temp, left_speed) = curve[index - 1];
        let (right_temp, right_speed) = curve[index];
        if temp_c <= right_temp {
            if mode == "step" {
                return left_speed;
            }
            let span = right_temp - left_temp;
            let t = (temp_c - left_temp) / span;
            return left_speed + (right_speed - left_speed) * t;
        }
    }
    curve[curve.len() - 1].1
}

/// `apply_hysteresis` — never hold back a spin-up; hold on gentle cooling.
pub fn apply_hysteresis(
    current_temp_c: f64,
    raw_target_speed: f64,
    last_temp_c: Option<f64>,
    last_speed: Option<f64>,
    hysteresis_c: f64,
) -> f64 {
    let (Some(last_temp_c), Some(last_speed)) = (last_temp_c, last_speed) else {
        return raw_target_speed;
    };
    if hysteresis_c <= 0.0 {
        return raw_target_speed;
    }
    if raw_target_speed >= last_speed {
        return raw_target_speed;
    }
    if current_temp_c > last_temp_c {
        return raw_target_speed;
    }
    if (last_temp_c - current_temp_c) < hysteresis_c {
        return last_speed;
    }
    raw_target_speed
}

/// `limit_speed_change` — slew-rate limiter.
pub fn limit_speed_change(
    target_speed: f64,
    last_speed: Option<f64>,
    elapsed_s: f64,
    max_step_up: f64,
    max_step_down: f64,
) -> f64 {
    let Some(last_speed) = last_speed else {
        return target_speed;
    };
    if elapsed_s <= 0.0 {
        return target_speed;
    }
    let mut limited = target_speed;
    if limited > last_speed && max_step_up > 0.0 {
        limited = limited.min(last_speed + max_step_up * elapsed_s);
    }
    if limited < last_speed && max_step_down > 0.0 {
        limited = limited.max(last_speed - max_step_down * elapsed_s);
    }
    limited
}

/// `format_curve_points` — journal display only.
pub fn format_curve_points(curve: &[(f64, f64)]) -> String {
    curve
        .iter()
        .map(|&(t, s)| format!("{t:.0}C->{s:.0}%"))
        .collect::<Vec<_>>()
        .join(", ")
}

/// `build_effective_manual_curve` — display/telemetry only.
pub fn build_effective_manual_curve(
    curve: &[(f64, f64)],
    manual_enable_temp_c: f64,
    eff_min: f64,
    eff_max: f64,
    mode: &str,
) -> Vec<(f64, f64)> {
    let start_speed = clamp(
        speed_for_temp(manual_enable_temp_c, curve, mode),
        eff_min,
        eff_max,
    );
    let mut effective = vec![(manual_enable_temp_c, start_speed)];
    for &(temp_c, speed_pct) in curve {
        if temp_c <= manual_enable_temp_c {
            continue;
        }
        let clamped = clamp(speed_pct, eff_min, eff_max);
        let last_speed = effective.last().unwrap().1;
        if (clamped - last_speed).abs() < 0.001 {
            continue;
        }
        effective.push((temp_c, clamped));
    }
    effective
}

// --- fan_config ------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FanConfig {
    pub poll_interval_s: f64,
    pub curve: Vec<(f64, f64)>,
    pub hysteresis_c: f64,
    pub mode: String,
    pub min_fan_speed_pct: i64,
    pub max_fan_speed_pct: i64,
    pub max_step_up_pct_per_s: f64,
    pub max_step_down_pct_per_s: f64,
    pub manual_enable_temp_c: f64,
    pub auto_restore_temp_c: f64,
    pub emergency_auto_override_temp_c: f64,
    pub emergency_auto_resume_temp_c: f64,
    pub force_update_every_poll: bool,
    pub curve_source: Option<String>,
    pub curve_source_path: Option<String>,
}

impl FanConfig {
    #[cfg(test)]
    pub fn defaults() -> Self {
        FanConfig {
            poll_interval_s: 2.0,
            curve: vec![(55.0, 30.0), (65.0, 35.0), (70.0, 40.0), (80.0, 45.0)],
            hysteresis_c: 2.0,
            mode: "linear".into(),
            min_fan_speed_pct: 20,
            max_fan_speed_pct: 100,
            max_step_up_pct_per_s: 25.0,
            max_step_down_pct_per_s: 15.0,
            manual_enable_temp_c: 55.0,
            auto_restore_temp_c: 50.0,
            emergency_auto_override_temp_c: 80.0,
            emergency_auto_resume_temp_c: 75.0,
            force_update_every_poll: false,
            curve_source: None,
            curve_source_path: None,
        }
    }
}

// --- runtime fan settings --------------------------------------------------

#[derive(Debug, Clone)]
pub struct RuntimeFanSettings {
    pub poll_interval_s: f64,
    pub curve: Vec<(f64, f64)>,
    pub effective_manual_curve: Vec<(f64, f64)>,
    pub hysteresis_c: f64,
    pub mode: String,
    pub effective_min_fan_speed_pct: i64,
    pub effective_max_fan_speed_pct: i64,
    pub max_step_up_pct_per_s: f64,
    pub max_step_down_pct_per_s: f64,
    pub manual_enable_temp_c: f64,
    pub auto_restore_temp_c: f64,
    pub emergency_auto_override_temp_c: f64,
    pub emergency_auto_resume_temp_c: f64,
    pub force_update_every_poll: bool,
}

impl RuntimeFanSettings {
    /// `_build_runtime_fan_settings` — validates the curve when fan control is on.
    pub fn build(fan_config: &FanConfig, fan_control_enabled: bool) -> Result<Self, String> {
        if !fan_control_enabled {
            return Ok(RuntimeFanSettings {
                poll_interval_s: fan_config.poll_interval_s,
                curve: Vec::new(),
                effective_manual_curve: Vec::new(),
                hysteresis_c: 0.0,
                mode: "linear".into(),
                effective_min_fan_speed_pct: 0,
                effective_max_fan_speed_pct: 100,
                max_step_up_pct_per_s: 0.0,
                max_step_down_pct_per_s: 0.0,
                manual_enable_temp_c: 0.0,
                auto_restore_temp_c: 0.0,
                emergency_auto_override_temp_c: 80.0,
                emergency_auto_resume_temp_c: 75.0,
                force_update_every_poll: false,
            });
        }
        let curve = fan_config.curve.clone();
        validate_curve(&curve)?;
        Ok(RuntimeFanSettings {
            poll_interval_s: fan_config.poll_interval_s,
            curve,
            effective_manual_curve: Vec::new(),
            hysteresis_c: fan_config.hysteresis_c,
            mode: fan_config.mode.clone(),
            effective_min_fan_speed_pct: fan_config.min_fan_speed_pct,
            effective_max_fan_speed_pct: fan_config.max_fan_speed_pct,
            max_step_up_pct_per_s: fan_config.max_step_up_pct_per_s,
            max_step_down_pct_per_s: fan_config.max_step_down_pct_per_s,
            manual_enable_temp_c: fan_config.manual_enable_temp_c,
            auto_restore_temp_c: fan_config.auto_restore_temp_c,
            emergency_auto_override_temp_c: fan_config.emergency_auto_override_temp_c,
            emergency_auto_resume_temp_c: fan_config.emergency_auto_resume_temp_c,
            force_update_every_poll: fan_config.force_update_every_poll,
        })
    }

    /// `_apply_device_fan_limits`.
    pub fn apply_device_fan_limits(
        &mut self,
        device_min: Option<u32>,
        device_max: Option<u32>,
    ) -> Result<(), String> {
        if let Some(min) = device_min {
            self.effective_min_fan_speed_pct = self.effective_min_fan_speed_pct.max(i64::from(min));
        }
        if let Some(max) = device_max {
            self.effective_max_fan_speed_pct = self.effective_max_fan_speed_pct.min(i64::from(max));
        }
        if self.effective_max_fan_speed_pct < self.effective_min_fan_speed_pct {
            return Err("effective fan speed range is invalid".to_string());
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validate_curve_errors() {
        assert_eq!(
            validate_curve(&[(50.0, 30.0)]).unwrap_err(),
            "curve must contain at least two points"
        );
        assert_eq!(
            validate_curve(&[(50.0, 30.0), (50.0, 40.0)]).unwrap_err(),
            "curve temperatures must be strictly increasing"
        );
        assert_eq!(
            validate_curve(&[(50.0, 30.0), (60.0, 120.0)]).unwrap_err(),
            "curve fan speeds must be in the range 0..100"
        );
        assert_eq!(
            validate_curve(&[(50.0, 40.0), (60.0, 30.0)]).unwrap_err(),
            "curve fan speeds must not decrease as temperature rises"
        );
        assert!(validate_curve(&[(50.0, 30.0), (60.0, 30.0), (70.0, 45.0)]).is_ok());
    }

    #[test]
    fn interpolation_edges() {
        let curve = [(50.0, 30.0), (60.0, 40.0), (70.0, 50.0)];
        assert_eq!(speed_for_temp(40.0, &curve, "linear"), 30.0); // below first
        assert_eq!(speed_for_temp(80.0, &curve, "linear"), 50.0); // above last
        assert_eq!(speed_for_temp(55.0, &curve, "linear"), 35.0); // midpoint
        assert_eq!(speed_for_temp(55.0, &curve, "step"), 30.0); // step = left
        assert_eq!(speed_for_temp(50.0, &curve, "linear"), 30.0); // exactly first
    }

    #[test]
    fn hysteresis_paths() {
        // First sample (no last) → raw.
        assert_eq!(apply_hysteresis(60.0, 40.0, None, None, 2.0), 40.0);
        // Spin-up never held.
        assert_eq!(
            apply_hysteresis(60.0, 50.0, Some(58.0), Some(40.0), 2.0),
            50.0
        );
        // Temp rising → no cooling hysteresis.
        assert_eq!(
            apply_hysteresis(59.0, 30.0, Some(58.0), Some(40.0), 2.0),
            30.0
        );
        // Cooling but within band → hold previous.
        assert_eq!(
            apply_hysteresis(57.0, 30.0, Some(58.0), Some(40.0), 2.0),
            40.0
        );
        // Cooled past band → allow spin-down.
        assert_eq!(
            apply_hysteresis(55.0, 30.0, Some(58.0), Some(40.0), 2.0),
            30.0
        );
    }

    /// Full fan-decision chain (raw → hysteresis → slew → round+clamp) over a
    /// temp/time sequence. Expected targets generated by running the Python
    /// originals directly:
    ///   python3 -c 'from runtime.fan_control.fan_curve_runtime_rules import
    ///   clamp, speed_for_temp, apply_hysteresis, limit_speed_change; ...' → [40,46,42,36,36,26]
    #[test]
    fn fan_decision_chain_matches_python() {
        let curve = [(45.0, 0.0), (60.0, 30.0), (70.0, 50.0), (90.0, 100.0)];
        let (emin, emax) = (0.0, 100.0);
        let (hy, up, down) = (2.0, 25.0, 15.0);
        let seq = [
            (65.0, 0.0),
            (68.0, 1.0),
            (66.0, 2.0),
            (63.0, 3.0),
            (63.0, 10.0),
            (58.0, 11.0),
        ];
        let mut last_speed: Option<f64> = None;
        let mut last_temp: Option<f64> = None;
        let mut last_update = 0.0;
        let mut out = Vec::new();
        for (temp, t) in seq {
            let raw = clamp(speed_for_temp(temp, &curve, "linear"), emin, emax);
            let hyst = apply_hysteresis(temp, raw, last_temp, last_speed, hy);
            let target = crate::gpu::round_half_even(clamp(
                limit_speed_change(hyst, last_speed, t - last_update, up, down),
                emin,
                emax,
            )) as i64;
            if Some(target as f64) != last_speed {
                last_temp = Some(temp);
                last_speed = Some(target as f64);
                last_update = t;
            }
            out.push(target);
        }
        assert_eq!(out, vec![40, 46, 42, 36, 36, 26]);
    }

    #[test]
    fn slew_limiter() {
        // No last → target.
        assert_eq!(limit_speed_change(50.0, None, 1.0, 25.0, 15.0), 50.0);
        // Up capped at last + up*elapsed.
        assert_eq!(limit_speed_change(50.0, Some(20.0), 1.0, 25.0, 15.0), 45.0);
        // Down capped at last - down*elapsed.
        assert_eq!(limit_speed_change(0.0, Some(40.0), 1.0, 25.0, 15.0), 25.0);
        // elapsed <= 0 → target unchanged.
        assert_eq!(limit_speed_change(50.0, Some(20.0), 0.0, 25.0, 15.0), 50.0);
    }
}
