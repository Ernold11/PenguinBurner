//! Fan-control math + saved-curve loading — port of
//! `runtime/fan_control/fan_curve_runtime_rules.py`,
//! `runtime/fan_control/auto_uv_saved_fan_curve.py`, and the settings build in
//! `runtime_loop.py`. Pure math; error strings are load-bearing.

use std::path::Path;

use serde_json::Value;

use crate::paths;

/// `AUTO_UV_FAN_TUNING` (auto_uv/domain/user_options.py).
pub mod tuning {
    pub const MAX_BASE_CURVE_LOAD_TEMP_C: f64 = 75.0;
    pub const MAX_CURVE_POINTS: usize = 10;
    pub const ZERO_RPM_UNTIL_TEMP_C: f64 = 45.0;
    pub const MINIMUM_ACTIVE_TEMP_C: f64 = 60.0;
    pub const MINIMUM_ACTIVE_SPEED_PCT: f64 = 30.0;
    pub const EMERGENCY_TEMP_C: f64 = 80.0;
    pub const EMERGENCY_MIN_SPEED_PCT: f64 = 75.0;
    pub const FULL_SPEED_TEMP_C: f64 = 90.0;
    pub const FULL_SPEED_PCT: f64 = 100.0;
    pub const HARDWARE_AUTO_OVERRIDE_TEMP_C: f64 = 95.0;
    pub const EMERGENCY_RESUME_TEMP_C: f64 = 80.0;
    pub const POLL_INTERVAL_S: f64 = 2.0;
    pub const HYSTERESIS_C: f64 = 2.0;
    pub const MIN_SPEED_PCT: i64 = 0;
    pub const MAX_SPEED_PCT: i64 = 100;
    pub const MAX_STEP_UP_PCT_PER_S: f64 = 25.0;
    pub const MAX_STEP_DOWN_PCT_PER_S: f64 = 15.0;
}

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

#[derive(Debug, Clone)]
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
    /// The `default_runtime_config()["fan"]` defaults.
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

    /// Overlay `[fan]` TOML values over the defaults (parity with
    /// `config[section].update(loaded)`).
    pub fn from_runtime_config(doc: &super::config::TomlDoc) -> Self {
        let mut config = FanConfig::defaults();
        let Some(section) = doc.get("fan") else {
            return config;
        };
        use super::config::TomlValue;
        // Overlay each scalar `[fan]` key onto the defaults (key == field name).
        // mode + curve stay explicit below (String conversion / nested-array parse).
        macro_rules! overlay {
            ($field:ident, $conv:ident) => {
                if let Some(v) = section.get(stringify!($field)).and_then(TomlValue::$conv) {
                    config.$field = v;
                }
            };
        }
        overlay!(poll_interval_s, as_f64);
        overlay!(hysteresis_c, as_f64);
        overlay!(min_fan_speed_pct, as_i64);
        overlay!(max_fan_speed_pct, as_i64);
        overlay!(max_step_up_pct_per_s, as_f64);
        overlay!(max_step_down_pct_per_s, as_f64);
        overlay!(manual_enable_temp_c, as_f64);
        overlay!(auto_restore_temp_c, as_f64);
        overlay!(emergency_auto_override_temp_c, as_f64);
        overlay!(emergency_auto_resume_temp_c, as_f64);
        overlay!(force_update_every_poll, as_bool);
        if let Some(v) = section.get("mode").and_then(TomlValue::as_str) {
            config.mode = v.to_string();
        }
        if let Some(array) = section.get("curve").and_then(TomlValue::as_array) {
            let mut curve = Vec::new();
            for point in array {
                if let Some(pair) = point.as_array() {
                    if pair.len() == 2 {
                        if let (Some(t), Some(s)) = (pair[0].as_f64(), pair[1].as_f64()) {
                            curve.push((t, s));
                        }
                    }
                }
            }
            if !curve.is_empty() {
                config.curve = curve;
            }
        }
        config
    }
}

// --- saved auto-UV silent fan curve ----------------------------------------

/// Loading a saved silent fan curve either downgrades fan control (parity) via
/// one of these two labels, or replaces the fan config.
pub enum SavedFanCurve {
    Loaded(FanConfig),
    Blocked(String),
    Invalid(String),
}

fn f_json(value: &Value) -> Option<f64> {
    value.as_f64()
}

/// `validate_auto_uv_fan_curve_safety` — FanCurveBlockedError strings verbatim.
fn validate_safety(curve: &[(f64, f64)], path: &Path) -> Result<(), String> {
    use tuning::*;
    let path = path.display();
    if curve.len() > MAX_CURVE_POINTS {
        return Err(format!(
            "auto-UV fan curve has too many points: {} > {}: {}",
            curve.len(),
            MAX_CURVE_POINTS,
            path
        ));
    }
    let zero_speed = speed_for_temp(ZERO_RPM_UNTIL_TEMP_C, curve, "linear");
    if zero_speed > 0.01 {
        return Err(format!(
            "auto-UV fan curve unsafe at zero-rpm point: {ZERO_RPM_UNTIL_TEMP_C:.1}C is {zero_speed:.1}% instead of 0%: {path}"
        ));
    }
    let require = |temp: f64, min_speed: f64, label: &str| -> Result<(), String> {
        let speed = speed_for_temp(temp, curve, "linear");
        if speed + 0.01 < min_speed {
            return Err(format!(
                "auto-UV fan curve unsafe at {label}: {speed:.1}% < {min_speed:.1}%: {path}"
            ));
        }
        Ok(())
    };
    require(
        MINIMUM_ACTIVE_TEMP_C,
        MINIMUM_ACTIVE_SPEED_PCT,
        "active minimum",
    )?;
    require(
        MAX_BASE_CURVE_LOAD_TEMP_C,
        MINIMUM_ACTIVE_SPEED_PCT,
        "safe load target",
    )?;
    require(EMERGENCY_TEMP_C, EMERGENCY_MIN_SPEED_PCT, "emergency")?;
    require(FULL_SPEED_TEMP_C, FULL_SPEED_PCT, "full speed")?;
    if HARDWARE_AUTO_OVERRIDE_TEMP_C <= FULL_SPEED_TEMP_C {
        return Err(format!(
            "auto-UV fan curve hardware-auto override must be above the {FULL_SPEED_TEMP_C:.1}C full-speed point"
        ));
    }
    Ok(())
}

/// `load_auto_uv_fan_curve` — parse + validate the saved silent curve JSON.
/// Returns `SavedFanCurve`; `None` (file absent) is handled by the caller.
pub fn load_auto_uv_fan_curve(base: &FanConfig) -> Option<SavedFanCurve> {
    load_auto_uv_fan_curve_at(
        &paths::user_config_dir().join("auto-uv-fan-curve.json"),
        base,
    )
}

fn load_auto_uv_fan_curve_at(path: &Path, base: &FanConfig) -> Option<SavedFanCurve> {
    if !path.is_file() {
        return None;
    }
    let path_display = path.display().to_string();
    let bytes = std::fs::read(path).ok()?;
    let payload: Value = match serde_json::from_str(&String::from_utf8_lossy(&bytes)) {
        Ok(v) => v,
        Err(_) => {
            return Some(SavedFanCurve::Invalid(format!(
                "auto-UV fan curve payload is invalid: {path_display}"
            )))
        }
    };
    let Some(obj) = payload.as_object() else {
        return Some(SavedFanCurve::Invalid(format!(
            "auto-UV fan curve payload is invalid: {path_display}"
        )));
    };
    let max_base = tuning::MAX_BASE_CURVE_LOAD_TEMP_C;
    let loaded_temp_c = obj.get("loaded_temperature_c");
    // Full Python truthiness (`bool(payload.get("fan_curve_blocked"))`): this is
    // a thermal-safety flag, so `"true"`, `"yes"`, `1.0`, … must all block —
    // never load a curve Python would have refused.
    let blocked = super::profile_store::is_truthy(obj.get("fan_curve_blocked"));
    if blocked {
        let reason = obj
            .get("block_reason")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .unwrap_or("unknown");
        let temp_text = match loaded_temp_c {
            None | Some(Value::Null) => "n/a".to_string(),
            Some(v) => match f_json(v) {
                Some(t) => format!("{t:.1}C"),
                None => "invalid".to_string(),
            },
        };
        return Some(SavedFanCurve::Blocked(format!(
            "auto-UV fan curve is blocked: reason={reason} loaded-temp={temp_text} limit={max_base:.1}C"
        )));
    }
    match loaded_temp_c {
        None | Some(Value::Null) => {
            return Some(SavedFanCurve::Blocked(
                "auto-UV fan curve rejected: missing saved final load temperature".to_string(),
            ));
        }
        Some(v) => match f_json(v) {
            None => {
                return Some(SavedFanCurve::Invalid(format!(
                    "auto-UV fan curve loaded temperature is invalid: {path_display}"
                )))
            }
            Some(loaded_temp) => {
                if loaded_temp > max_base {
                    return Some(SavedFanCurve::Blocked(format!(
                        "auto-UV fan curve rejected: saved final load temperature {loaded_temp:.1}C is above the {max_base:.1}C safety limit"
                    )));
                }
            }
        },
    }
    let Some(raw_fan) = obj.get("fan").and_then(Value::as_object) else {
        return Some(SavedFanCurve::Invalid(format!(
            "auto-UV fan curve has no fan section: {path_display}"
        )));
    };
    let raw_curve = raw_fan.get("curve").and_then(Value::as_array);
    let raw_curve = match raw_curve {
        Some(c) if !c.is_empty() => c,
        _ => {
            return Some(SavedFanCurve::Invalid(format!(
                "auto-UV fan curve has no curve points: {path_display}"
            )))
        }
    };
    let mut curve = Vec::new();
    for raw_point in raw_curve {
        let pair = raw_point.as_array();
        let ok = pair
            .filter(|p| p.len() == 2)
            .and_then(|p| Some((f_json(&p[0])?, f_json(&p[1])?)));
        match ok {
            Some(point) => curve.push(point),
            None => {
                return Some(SavedFanCurve::Invalid(format!(
                    "auto-UV fan curve point is invalid: {path_display}: {raw_point}"
                )))
            }
        }
    }
    if let Err(msg) = validate_curve(&curve) {
        // validate_curve raises NvmlError → "invalid" path.
        return Some(SavedFanCurve::Invalid(msg));
    }
    if let Err(msg) = validate_safety(&curve, path) {
        return Some(SavedFanCurve::Blocked(msg));
    }
    let mut config = base.clone();
    config.poll_interval_s = tuning::POLL_INTERVAL_S;
    config.hysteresis_c = tuning::HYSTERESIS_C;
    config.mode = "linear".into();
    config.min_fan_speed_pct = tuning::MIN_SPEED_PCT;
    config.max_fan_speed_pct = tuning::MAX_SPEED_PCT;
    config.max_step_up_pct_per_s = tuning::MAX_STEP_UP_PCT_PER_S;
    config.max_step_down_pct_per_s = tuning::MAX_STEP_DOWN_PCT_PER_S;
    config.manual_enable_temp_c = tuning::MINIMUM_ACTIVE_TEMP_C;
    config.auto_restore_temp_c = tuning::ZERO_RPM_UNTIL_TEMP_C;
    config.emergency_auto_override_temp_c = tuning::HARDWARE_AUTO_OVERRIDE_TEMP_C;
    config.emergency_auto_resume_temp_c = tuning::EMERGENCY_RESUME_TEMP_C;
    config.force_update_every_poll = false;
    config.curve = curve;
    config.curve_source = Some("auto-uv".into());
    config.curve_source_path = Some(path_display);
    Some(SavedFanCurve::Loaded(config))
}

/// `_prepare_runtime_fan_source`: resolve the effective fan config + control flag,
/// logging the exact downgrade messages on a blocked/invalid saved curve.
pub fn prepare_fan_source(
    base: &FanConfig,
    fan_control_enabled: bool,
    log: &mut dyn FnMut(&str),
) -> (FanConfig, bool) {
    if !fan_control_enabled {
        return (base.clone(), false);
    }
    let path = paths::user_config_dir().join("auto-uv-fan-curve.json");
    if !path.is_file() {
        return (base.clone(), true);
    }
    match load_auto_uv_fan_curve(base) {
        Some(SavedFanCurve::Loaded(config)) => (config, true),
        Some(SavedFanCurve::Blocked(msg)) => {
            log(&format!(
                "Manual fan control disabled by auto-UV safety guard: {msg}"
            ));
            (base.clone(), false)
        }
        Some(SavedFanCurve::Invalid(msg)) => {
            log(&format!(
                "Manual fan control disabled because the auto-UV fan curve is present but invalid: path={} error={msg}",
                path.display()
            ));
            (base.clone(), false)
        }
        None => {
            log(&format!(
                "Manual fan control disabled because the auto-UV fan curve file could not be loaded: path={}",
                path.display()
            ));
            (base.clone(), false)
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

    /// `fan_curve_blocked` uses full Python truthiness (safety flag): string /
    /// float truthy values must block — previously `"true"` / `1.0` bypassed it.
    #[test]
    fn saved_curve_blocked_flag_matches_python_truthiness() {
        let dir = std::env::temp_dir().join(format!("pb-fancurve-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("auto-uv-fan-curve.json");
        let base = FanConfig::defaults();
        let load = |payload: &str| {
            std::fs::write(&path, payload).unwrap();
            load_auto_uv_fan_curve_at(&path, &base)
        };

        // Truthy variants — including the ones the old parser let through.
        for value in ["true", "1", "1.0", r#""true""#, r#""yes""#, r#""0""#, "[1]"] {
            let payload = format!(r#"{{"fan_curve_blocked": {value}, "block_reason": "too hot"}}"#);
            match load(&payload) {
                Some(SavedFanCurve::Blocked(msg)) => assert!(
                    msg.starts_with("auto-UV fan curve is blocked: reason=too hot"),
                    "value {value}: {msg}"
                ),
                _ => panic!("value {value} must take the blocked-flag branch"),
            }
        }
        // Falsy variants skip the blocked-flag branch (and then fail on the
        // missing load temperature — a different message).
        for value in ["false", "0", "0.0", r#""""#, "null", "[]"] {
            let payload = format!(r#"{{"fan_curve_blocked": {value}}}"#);
            match load(&payload) {
                Some(SavedFanCurve::Blocked(msg)) => assert!(
                    msg.contains("missing saved final load temperature"),
                    "value {value} must not hit the blocked-flag branch: {msg}"
                ),
                _ => panic!("unexpected result for value {value}"),
            }
        }
        let _ = std::fs::remove_dir_all(&dir);
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
