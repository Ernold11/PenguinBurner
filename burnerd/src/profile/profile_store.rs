//! Saved-profile loading, tier classification, and adaptive tier resolution —
//! port of `profiles/uv/profile_store.py`, `profile_tiers.py`, and
//! `runtime_auto_uv_profile.py`. Backward compatible with every legacy field
//! alias (spec 06 §8.7); `format_version` is written but never gates reads.

use std::path::{Path, PathBuf};

use serde_json::{Map, Value};

use crate::paths;

pub const STOCK_PROFILE_SELECTOR: &str = "__stock__";

pub const PROFILE_TIER_EFFICIENCY: &str = "efficiency";
pub const PROFILE_TIER_BALANCED: &str = "balanced";
pub const PROFILE_TIER_PERFORMANCE: &str = "performance";
pub const PROFILE_TIERS: [&str; 3] = [
    PROFILE_TIER_EFFICIENCY,
    PROFILE_TIER_BALANCED,
    PROFILE_TIER_PERFORMANCE,
];
const BALANCED_TAIL_RISE_BINS: i64 = 4;
const PERFORMANCE_TAIL_RISE_BINS: i64 = 6;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PlanItem {
    pub index: u32,
    pub voltage_mv: i64,
    pub base_mhz: i64,
    pub target_mhz: i64,
    pub new_offset_mhz: i64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct FlattenTarget {
    pub source: String,
    pub lock_clock_mhz: i64,
    pub lock_voltage_mv: Option<i64>,
    pub end_voltage_mv: Option<i64>,
    pub tail_point_count: Option<i64>,
    pub ceiling_clock_mhz: Option<i64>,
    pub tail_rise_bins: Option<i64>,
}

#[derive(Debug, Clone)]
pub struct LoadedCurve {
    pub path: PathBuf,
    pub profile_id: String,
    pub profile_tier: String,
    pub profile_tier_key: String,
    pub plan: Vec<PlanItem>,
    pub lock_clock_mhz: i64,
    pub candidate_voltage_mv: i64,
    pub memory_offset_mhz: Option<i64>,
    pub power_limit_w: Option<i64>,
    pub flatten_target: FlattenTarget,
}

// --- tier normalization -----------------------------------------------------

/// `normalize_profile_tier` (aliases → canonical tier, else default).
pub fn normalize_profile_tier(value: &str, default: &str) -> String {
    let text = value.trim().to_lowercase();
    if text.is_empty() {
        return default.to_string();
    }
    let mapped = match text.as_str() {
        "eff" | "efficiency" => PROFILE_TIER_EFFICIENCY,
        "balanced" | "balance" | "bal" | "normal" => PROFILE_TIER_BALANCED,
        "perf" | "performance" | "aggressive" => PROFILE_TIER_PERFORMANCE,
        _ => return default.to_string(),
    };
    mapped.to_string()
}

/// `profile_tier_label` — `"Efficiency"`/`"Balanced"`/`"Performance"` or `""`.
pub fn profile_tier_label(value: &str) -> String {
    match normalize_profile_tier(value, "").as_str() {
        PROFILE_TIER_EFFICIENCY => "Efficiency",
        PROFILE_TIER_BALANCED => "Balanced",
        PROFILE_TIER_PERFORMANCE => "Performance",
        _ => "",
    }
    .to_string()
}

// --- JSON value coercion ----------------------------------------------------

fn is_empty_value(v: &Value) -> bool {
    v.is_null() || matches!(v, Value::String(s) if s.is_empty())
}

/// Python `int(value)`: truncates floats toward zero, parses int strings.
fn value_int(v: &Value) -> Option<i64> {
    match v {
        Value::Number(n) => n.as_i64().or_else(|| n.as_f64().map(|f| f.trunc() as i64)),
        Value::String(s) => s.trim().parse::<i64>().ok(),
        Value::Bool(b) => Some(i64::from(*b)),
        _ => None,
    }
}

fn value_float(v: &Value) -> Option<f64> {
    match v {
        Value::Number(n) => n.as_f64(),
        Value::String(s) => s.trim().parse::<f64>().ok(),
        Value::Bool(b) => Some(if *b { 1.0 } else { 0.0 }),
        _ => None,
    }
}

fn value_display(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Number(n) => n.to_string(),
        Value::Bool(b) => if *b { "True" } else { "False" }.to_string(),
        Value::Null => "None".to_string(),
        _ => v.to_string(),
    }
}

fn get_str(map: &Map<String, Value>, key: &str) -> String {
    map.get(key).map(value_display).unwrap_or_default()
}

/// Full Python truthiness (`bool(value)`) over a JSON value. Also used by the
/// saved-fan-curve loader for the `fan_curve_blocked` safety flag, which must
/// not be bypassable by `"true"` / `1.0`-style values Python would block on.
pub(in crate::profile) fn is_truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64().is_some_and(|f| f != 0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

// --- profile id / candidate id ----------------------------------------------

fn safe_profile_id(value: &str) -> String {
    let mut out = String::new();
    let mut prev_dash = false;
    for c in value.trim().chars() {
        if c.is_ascii_alphanumeric() || c == '_' || c == '.' || c == '-' {
            out.push(c);
            prev_dash = false;
        } else if !prev_dash {
            out.push('-');
            prev_dash = true;
        }
    }
    let trimmed = out.trim_matches('-');
    if trimmed.is_empty() {
        "profile".to_string()
    } else {
        trimmed.to_string()
    }
}

fn candidate_id(payload: &Map<String, Value>) -> String {
    let candidate = payload
        .get("candidate_id")
        .map(value_display)
        .unwrap_or_default();
    let candidate = candidate.trim();
    if !candidate.is_empty() {
        return safe_profile_id(candidate);
    }
    let voltage = payload
        .get("candidate_voltage_mv")
        .or_else(|| payload.get("voltage_mv"))
        .map(value_display)
        .unwrap_or_else(|| "na".to_string());
    let clock = payload
        .get("lock_clock_mhz")
        .or_else(|| payload.get("clock_mhz"))
        .map(value_display)
        .unwrap_or_else(|| "na".to_string());
    safe_profile_id(&format!("{voltage}mv-{clock}mhz"))
}

// --- ISO timestamp → epoch (for newest-first sorting) -----------------------

fn parse_iso_to_epoch(s: &str) -> Option<f64> {
    let s = s.trim();
    let sep = s.find(['T', ' '])?;
    let date_part = &s[..sep];
    let rest = &s[sep + 1..];
    let mut date = date_part.split('-');
    let year: i32 = date.next()?.parse().ok()?;
    let month: i32 = date.next()?.parse().ok()?;
    let day: i32 = date.next()?.parse().ok()?;

    // Split the optional trailing timezone offset from the time.
    let (time_part, offset_secs) = if let Some(stripped) = rest.strip_suffix('Z') {
        (stripped, 0i64)
    } else {
        let mut split = None;
        for (i, c) in rest.char_indices() {
            if i > 0 && (c == '+' || c == '-') {
                split = Some((i, c));
                break;
            }
        }
        match split {
            Some((i, sign)) => {
                let off = &rest[i + 1..];
                let mut parts = off.split(':');
                let h: i64 = parts.next()?.parse().ok()?;
                let m: i64 = parts.next().unwrap_or("0").parse().ok()?;
                let secs = (h * 3600 + m * 60) * if sign == '-' { -1 } else { 1 };
                (&rest[..i], secs)
            }
            None => (rest, 0),
        }
    };

    let mut time = time_part.split(':');
    let hour: i32 = time.next()?.parse().ok()?;
    let minute: i32 = time.next().unwrap_or("0").parse().ok()?;
    let sec_field = time.next().unwrap_or("0");
    let (sec_whole, frac) = match sec_field.split_once('.') {
        Some((w, f)) => (w, format!("0.{f}").parse::<f64>().unwrap_or(0.0)),
        None => (sec_field, 0.0),
    };
    let second: i32 = sec_whole.parse().ok()?;

    // SAFETY: a zeroed tm is valid; timegm reads only the set fields.
    let mut tm: libc::tm = unsafe { std::mem::zeroed() };
    tm.tm_year = year - 1900;
    tm.tm_mon = month - 1;
    tm.tm_mday = day;
    tm.tm_hour = hour;
    tm.tm_min = minute;
    tm.tm_sec = second;
    // SAFETY: tm is fully initialized above.
    let utc = unsafe { libc::timegm(&mut tm) };
    Some(utc as f64 - offset_secs as f64 + frac)
}

fn file_mtime_epoch(path: &Path) -> Option<f64> {
    let modified = std::fs::metadata(path).ok()?.modified().ok()?;
    modified
        .duration_since(std::time::UNIX_EPOCH)
        .ok()
        .map(|d| d.as_secs_f64())
}

fn profile_sort_time(profile: &Map<String, Value>) -> f64 {
    for key in ["profile_created_at", "verified_at", "created_at"] {
        let value = get_str(profile, key);
        let value = value.trim();
        if value.is_empty() {
            continue;
        }
        if let Some(epoch) = parse_iso_to_epoch(value) {
            return epoch;
        }
    }
    let path = get_str(profile, "path");
    file_mtime_epoch(Path::new(&path)).unwrap_or(0.0)
}

// --- reading + normalization ------------------------------------------------

fn read_json_object(path: &Path) -> Option<Map<String, Value>> {
    let bytes = std::fs::read(path).ok()?;
    let text = String::from_utf8_lossy(&bytes);
    let value: Value = serde_json::from_str(&text).ok()?;
    match value {
        Value::Object(map) => Some(map),
        _ => None,
    }
}

fn is_final_verified(payload: &Map<String, Value>) -> bool {
    is_truthy(payload.get("final_verified"))
}

fn is_user_edited(payload: &Map<String, Value>) -> bool {
    get_str(payload, "profile_source").trim() == "user-edited"
}

fn profile_is_visible(payload: &Map<String, Value>, include_unverified: bool) -> bool {
    if is_final_verified(payload) {
        return true;
    }
    include_unverified
        && is_user_edited(payload)
        && payload
            .get("requires_verification")
            .map_or(true, |v| is_truthy(Some(v)))
}

fn profile_is_runnable(payload: &Map<String, Value>, allow_unverified: bool) -> bool {
    is_final_verified(payload) || (allow_unverified && is_user_edited(payload))
}

fn file_time_iso(path: &Path) -> String {
    // Only the ordering matters downstream; a plain epoch string preserves it.
    file_mtime_epoch(path)
        .map(|e| e.to_string())
        .unwrap_or_default()
}

fn normalize_profile_payload(
    payload: &Map<String, Value>,
    path: &Path,
    source: &str,
    include_unverified: bool,
) -> Option<Map<String, Value>> {
    let has_points = matches!(payload.get("points"), Some(Value::Array(_)));
    let has_plan = matches!(payload.get("plan"), Some(Value::Array(_)));
    if !has_points && !has_plan {
        return None;
    }
    let candidate_voltage = payload
        .get("candidate_voltage_mv")
        .or_else(|| payload.get("voltage_mv"));
    let lock_clock = payload
        .get("lock_clock_mhz")
        .or_else(|| payload.get("clock_mhz"));
    if candidate_voltage.map_or(true, is_empty_value) || lock_clock.map_or(true, is_empty_value) {
        return None;
    }
    if !profile_is_visible(payload, include_unverified) {
        return None;
    }
    let mut profile = payload.clone();
    profile
        .entry("profile_source".to_string())
        .or_insert_with(|| Value::String(source.to_string()));
    profile.insert(
        "path".to_string(),
        Value::String(path.display().to_string()),
    );
    let cid = candidate_id(&profile);
    profile
        .entry("candidate_id".to_string())
        .or_insert_with(|| Value::String(cid.clone()));
    let pid = safe_profile_id(&get_str(&profile, "candidate_id"));
    profile
        .entry("profile_id".to_string())
        .or_insert_with(|| Value::String(pid));
    profile
        .entry("profile_created_at".to_string())
        .or_insert_with(|| Value::String(file_time_iso(path)));
    Some(profile)
}

/// `read_auto_uv_profiles`: glob `auto-uv-profiles/*.json` (sorted by name),
/// normalize, then sort by sort-time descending (newest first).
pub fn read_auto_uv_profiles(include_unverified: bool) -> Vec<Map<String, Value>> {
    let dir = paths::user_config_dir().join("auto-uv-profiles");
    let mut paths_list: Vec<PathBuf> = match std::fs::read_dir(&dir) {
        Ok(entries) => entries
            .filter_map(Result::ok)
            .map(|e| e.path())
            .filter(|p| p.extension().is_some_and(|ext| ext == "json"))
            .collect(),
        Err(_) => Vec::new(),
    };
    paths_list.sort();
    let mut profiles: Vec<Map<String, Value>> = paths_list
        .iter()
        .filter_map(|path| {
            let payload = read_json_object(path)?;
            normalize_profile_payload(&payload, path, "profile-store", include_unverified)
        })
        .collect();
    profiles.sort_by(|a, b| {
        profile_sort_time(b)
            .partial_cmp(&profile_sort_time(a))
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    profiles
}

/// `resolve_auto_uv_profile` → (path, payload). `latest`/`active` = newest.
pub fn resolve_auto_uv_profile(
    selector: &str,
    allow_unverified: bool,
) -> Option<(PathBuf, Map<String, Value>)> {
    let text = selector.trim();
    if text.is_empty() {
        return None;
    }
    if text == "active" || text == "latest" {
        let profiles = read_auto_uv_profiles(allow_unverified);
        let profile = profiles.into_iter().next()?;
        let path = PathBuf::from(get_str(&profile, "path"));
        return Some((path, profile));
    }
    let path = PathBuf::from(text);
    if path.is_file() {
        let payload = read_json_object(&path)?;
        if !profile_is_runnable(&payload, allow_unverified) {
            return None;
        }
        return Some((path, payload));
    }
    for profile in read_auto_uv_profiles(allow_unverified) {
        let profile_path = get_str(&profile, "path");
        let path = Path::new(&profile_path);
        let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
        let stem = path.file_stem().and_then(|n| n.to_str()).unwrap_or("");
        if text == get_str(&profile, "profile_id")
            || text == get_str(&profile, "candidate_id")
            || text == name
            || text == stem
        {
            return Some((PathBuf::from(&profile_path), profile));
        }
    }
    None
}

// --- generated tier classification ------------------------------------------

fn optional_int(v: Option<&Value>) -> Option<i64> {
    match v {
        None | Some(Value::Null) => None,
        Some(Value::String(s)) if s.is_empty() => None,
        Some(value) => value_int(value),
    }
}

/// `generated_profile_tier` (profile_tiers.py:79).
pub fn generated_profile_tier(profile: &Map<String, Value>) -> String {
    for key in [
        "generated_profile_tier",
        "profile_generated_tier",
        "auto_uv_requested_mode",
        "auto_uv_profile_tier",
        "profile_tier",
    ] {
        let tier = normalize_profile_tier(&get_str(profile, key), "");
        if !tier.is_empty() {
            return tier;
        }
    }
    let mode = normalize_profile_tier(&get_str(profile, "auto_uv_mode"), "");
    if mode == PROFILE_TIER_PERFORMANCE {
        return PROFILE_TIER_PERFORMANCE.to_string();
    }
    let profile_curve = get_str(profile, "profile_curve").trim().to_lowercase();
    if profile_curve == "performance-sweep" || profile_curve == "auto-oc" {
        return PROFILE_TIER_PERFORMANCE.to_string();
    }
    let mut tail_rise_bins = optional_int(profile.get("tail_rise_bins"));
    if tail_rise_bins.is_none() {
        if let Some(Value::Object(ft)) = profile.get("flatten_target") {
            tail_rise_bins = optional_int(ft.get("tail_rise_bins"));
        }
    }
    if let Some(bins) = tail_rise_bins {
        return if bins >= PERFORMANCE_TAIL_RISE_BINS {
            PROFILE_TIER_PERFORMANCE
        } else if bins >= BALANCED_TAIL_RISE_BINS {
            PROFILE_TIER_BALANCED
        } else {
            PROFILE_TIER_EFFICIENCY
        }
        .to_string();
    }
    if mode == PROFILE_TIER_EFFICIENCY {
        PROFILE_TIER_EFFICIENCY.to_string()
    } else {
        PROFILE_TIER_BALANCED.to_string()
    }
}

// --- tier assignments -------------------------------------------------------

fn tier_assignments_path() -> PathBuf {
    paths::user_config_dir().join("auto-uv-profile-tier-assignments.json")
}

fn read_tier_assignment_payload() -> Map<String, Value> {
    read_json_object(&tier_assignments_path()).unwrap_or_default()
}

fn disabled_profile_ids(payload: &Map<String, Value>) -> Vec<String> {
    match payload.get("disabled_profile_ids") {
        Some(Value::Array(a)) => a
            .iter()
            .map(value_display)
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .collect(),
        _ => Vec::new(),
    }
}

/// `load_profile_tier_assignments` → tier → profile_id.
pub fn load_profile_tier_assignments() -> Vec<(String, String)> {
    let payload = read_tier_assignment_payload();
    let Some(Value::Object(raw_tiers)) = payload.get("tiers") else {
        return Vec::new();
    };
    let disabled = disabled_profile_ids(&payload);
    let mut assignments = Vec::new();
    let mut seen: Vec<String> = Vec::new();
    for (raw_tier, raw_profile_id) in raw_tiers {
        let tier = normalize_profile_tier(raw_tier, "");
        let profile_id = value_display(raw_profile_id).trim().to_string();
        if tier.is_empty()
            || profile_id.is_empty()
            || seen.contains(&profile_id)
            || disabled.contains(&profile_id)
        {
            continue;
        }
        assignments.push((tier, profile_id.clone()));
        seen.push(profile_id);
    }
    assignments
}

/// Python `_truthy`: `str(value).strip().lower() in {"1","true","yes","on"}`
/// (a real `bool True` → `"true"`).
fn truthy_str(v: Option<&Value>) -> bool {
    let s = v.map(value_display).unwrap_or_default();
    matches!(
        s.trim().to_lowercase().as_str(),
        "1" | "true" | "yes" | "on"
    )
}

fn profile_tier_disabled(profile: &Map<String, Value>, disabled: &[String]) -> bool {
    if truthy_str(profile.get("profile_tier_disabled")) {
        return true;
    }
    let profile_id = get_str(profile, "profile_id");
    let profile_id = profile_id.trim();
    if profile_id.is_empty() {
        return false;
    }
    disabled.iter().any(|d| d == profile_id)
}

fn assigned_tier_for_profile(
    profile: &Map<String, Value>,
    assignments: &[(String, String)],
) -> String {
    let profile_id = get_str(profile, "profile_id");
    let profile_id = profile_id.trim();
    if profile_id.is_empty() {
        return String::new();
    }
    for (tier, assigned) in assignments {
        if assigned.trim() == profile_id {
            return normalize_profile_tier(tier, "");
        }
    }
    String::new()
}

/// `profile_tier_summary_fields` (only the two keys the runtime reads).
fn profile_tier_summary_fields(
    profile: &Map<String, Value>,
    assignments: &[(String, String)],
    disabled: &[String],
) -> (String, String) {
    let generated = generated_profile_tier(profile);
    let is_disabled = profile_tier_disabled(profile, disabled);
    let assigned = if is_disabled {
        String::new()
    } else {
        assigned_tier_for_profile(profile, assignments)
    };
    let effective = if is_disabled {
        String::new()
    } else if !assigned.is_empty() {
        assigned
    } else {
        generated
    };
    (profile_tier_label(&effective), effective)
}

/// `resolve_profile_tier_profiles` → tier → chosen profile (final-verified,
/// not disabled, assignment-first then generated-tier with balanced scoring).
pub fn resolve_profile_tier_profiles(
    profiles: &[Map<String, Value>],
) -> Vec<(String, Option<Map<String, Value>>)> {
    let assignments = load_profile_tier_assignments();
    let disabled = disabled_profile_ids(&read_tier_assignment_payload());

    let mut visible: Vec<Map<String, Value>> = profiles
        .iter()
        .filter(|p| is_final_verified(p) && !profile_tier_disabled(p, &disabled))
        .cloned()
        .collect();
    visible.sort_by(|a, b| {
        profile_sort_time(b)
            .partial_cmp(&profile_sort_time(a))
            .unwrap_or(std::cmp::Ordering::Equal)
    });

    let mut resolved: Vec<(String, Option<Map<String, Value>>)> = PROFILE_TIERS
        .iter()
        .map(|t| (t.to_string(), None))
        .collect();
    let mut assigned_ids: Vec<String> = Vec::new();

    for (i, tier) in PROFILE_TIERS.iter().enumerate() {
        let assigned_id = assignments
            .iter()
            .find(|(t, _)| t == tier)
            .map(|(_, id)| id.trim().to_string())
            .unwrap_or_default();
        if assigned_id.is_empty() {
            continue;
        }
        if let Some(profile) = visible
            .iter()
            .find(|p| get_str(p, "profile_id").trim() == assigned_id)
        {
            resolved[i].1 = Some(profile.clone());
            assigned_ids.push(assigned_id);
        }
    }

    let unassigned: Vec<&Map<String, Value>> = visible
        .iter()
        .filter(|p| {
            let id = get_str(p, "profile_id");
            let id = id.trim();
            !id.is_empty() && !assigned_ids.contains(&id.to_string())
        })
        .collect();

    for (i, tier) in PROFILE_TIERS.iter().enumerate() {
        if resolved[i].1.is_some() {
            continue;
        }
        let candidates: Vec<&Map<String, Value>> = unassigned
            .iter()
            .copied()
            .filter(|p| generated_profile_tier(p) == *tier)
            .collect();
        if candidates.is_empty() {
            continue;
        }
        // max by _profile_tier_sort_key (balanced score for the balanced tier).
        let chosen = candidates
            .iter()
            .copied()
            .max_by(|a, b| {
                profile_tier_sort_key(a, tier, &candidates)
                    .partial_cmp(&profile_tier_sort_key(b, tier, &candidates))
                    .unwrap_or(std::cmp::Ordering::Equal)
            })
            .unwrap();
        resolved[i].1 = Some(chosen.clone());
    }
    resolved
}

fn optional_float(v: Option<&Value>) -> Option<f64> {
    match v {
        None | Some(Value::Null) => None,
        Some(Value::String(s)) if s.is_empty() => None,
        Some(value) => value_float(value),
    }
}

fn profile_tier_sort_key(
    profile: &Map<String, Value>,
    tier: &str,
    candidates: &[&Map<String, Value>],
) -> (f64, f64) {
    if tier != PROFILE_TIER_BALANCED {
        return (0.0, profile_sort_time(profile));
    }
    (
        balanced_score(profile, candidates),
        profile_sort_time(profile),
    )
}

fn balanced_score(profile: &Map<String, Value>, candidates: &[&Map<String, Value>]) -> f64 {
    let best_fpsw = candidates
        .iter()
        .map(|c| optional_float(c.get("efficiency_fps_per_w")).unwrap_or(0.0))
        .fold(0.0_f64, f64::max);
    let best_fps = candidates
        .iter()
        .map(|c| optional_float(c.get("avg_fps")).unwrap_or(0.0))
        .fold(0.0_f64, f64::max);
    let relative = |value: Option<f64>, best: f64| -> f64 {
        match value {
            Some(v) if best > 0.0 => (v / best).max(0.0),
            _ => 0.0,
        }
    };
    let fpsw = relative(
        optional_float(profile.get("efficiency_fps_per_w")),
        best_fpsw,
    );
    let fps = relative(optional_float(profile.get("avg_fps")), best_fps);
    (fpsw + fps) / 2.0
}

/// `available_adaptive_tiers` — tiers with a resolved profile, deduped by id.
pub fn available_adaptive_tiers(resolved: &[(String, Option<Map<String, Value>>)]) -> Vec<String> {
    let mut tiers = Vec::new();
    let mut seen: Vec<String> = Vec::new();
    for tier in PROFILE_TIERS {
        let Some((_, Some(profile))) = resolved.iter().find(|(t, _)| t == tier) else {
            continue;
        };
        let profile_id = get_str(profile, "profile_id");
        let profile_id = profile_id.trim().to_string();
        if profile_id.is_empty() || seen.contains(&profile_id) {
            continue;
        }
        tiers.push(tier.to_string());
        seen.push(profile_id);
    }
    tiers
}

// --- tail ceiling + final curve loading -------------------------------------

/// `tail_ceiling_clock_mhz` (auto_uv/curve/rising_tail.py).
fn tail_ceiling_clock_mhz(plan: &[PlanItem], fallback: i64, lock_voltage_mv: i64) -> i64 {
    let mut ceiling = fallback;
    for point in plan {
        if point.voltage_mv < lock_voltage_mv {
            continue;
        }
        ceiling = ceiling.max(point.target_mhz);
    }
    ceiling
}

const MAX_AFTERBURNER_MEM_OFFSET_MHZ: i64 = 2000;

/// `profile_memory_offset_mhz` with `limit_mhz=None` (no static cap at load).
fn profile_memory_offset_mhz(payload: &Map<String, Value>, limit_mhz: Option<i64>) -> Option<i64> {
    for key in ["memory_offset_mhz", "mem_clk_vf_offset_mhz"] {
        let value = payload.get(key);
        match value {
            None | Some(Value::Null) => continue,
            Some(Value::String(s)) if s.is_empty() => continue,
            Some(value) => {
                let f = value_float(value)?;
                let mut offset = (super::round_half_even(f) as i64).max(0);
                if let Some(limit) = limit_mhz {
                    offset = offset.min(limit);
                }
                return Some(offset);
            }
        }
    }
    None
}

/// `profile_power_limit_w` — rounded, only if `> 0`.
fn profile_power_limit_w(payload: &Map<String, Value>) -> Option<i64> {
    let f = optional_float(payload.get("power_limit_w"))?;
    let power = super::round_half_even(f) as i64;
    (power > 0).then_some(power)
}

/// The first of two candidate strings that is non-empty (the profile-id fallback:
/// the profile's own field, else the resolved payload's field).
fn first_nonempty(primary: String, fallback: String) -> String {
    if primary.is_empty() {
        fallback
    } else {
        primary
    }
}

/// `load_auto_uv_final_curve` — the runtime loader. `Ok(None)` = stock / no
/// profile; `Err` mirrors the `NvmlError` message text.
pub fn load_auto_uv_final_curve(selector: &str) -> Result<Option<LoadedCurve>, String> {
    let selector = selector.trim();
    if selector == STOCK_PROFILE_SELECTOR {
        return Ok(None);
    }
    let lookup = if selector.is_empty() {
        "latest"
    } else {
        selector
    };
    let Some((path, resolved_payload)) = resolve_auto_uv_profile(lookup, false) else {
        if !selector.is_empty() {
            return Err(format!("Auto-UV profile not found: {selector}"));
        }
        return Ok(None);
    };
    if !path.is_file() {
        return Ok(None);
    }
    let Some(payload) = read_json_object(&path) else {
        return Err(format!(
            "auto-UV final curve has no V/F points: {}",
            path.display()
        ));
    };

    let raw_points = match payload.get("points") {
        Some(Value::Array(a)) if !a.is_empty() => a,
        _ => match payload.get("plan") {
            Some(Value::Array(a)) if !a.is_empty() => a,
            _ => {
                return Err(format!(
                    "auto-UV final curve has no V/F points: {}",
                    path.display()
                ))
            }
        },
    };

    let mut plan = Vec::with_capacity(raw_points.len());
    for raw in raw_points {
        let Some(obj) = raw.as_object() else {
            return Err(format!(
                "auto-UV final curve contains a non-object point: {}",
                path.display()
            ));
        };
        let read = |key: &str| obj.get(key).and_then(value_int);
        match (
            read("index"),
            read("voltage_mv"),
            read("base_mhz"),
            read("target_mhz"),
            read("new_offset_mhz"),
        ) {
            (
                Some(index),
                Some(voltage_mv),
                Some(base_mhz),
                Some(target_mhz),
                Some(new_offset_mhz),
            ) if index >= 0 => {
                plan.push(PlanItem {
                    index: index as u32,
                    voltage_mv,
                    base_mhz,
                    target_mhz,
                    new_offset_mhz,
                });
            }
            _ => {
                return Err(format!(
                    "auto-UV final curve point is invalid: {}: {}",
                    path.display(),
                    raw
                ))
            }
        }
    }

    let lock_clock_mhz = payload
        .get("lock_clock_mhz")
        .filter(|v| !is_empty_value(v))
        .and_then(value_int)
        .unwrap_or(0);
    let candidate_voltage_mv = payload
        .get("candidate_voltage_mv")
        .filter(|v| !is_empty_value(v))
        .and_then(value_int)
        .unwrap_or(0);
    if lock_clock_mhz <= 0 || candidate_voltage_mv <= 0 {
        return Err(format!(
            "auto-UV final curve is missing lock clock or voltage: {}",
            path.display()
        ));
    }
    let memory_offset_mhz = profile_memory_offset_mhz(&payload, None);
    let power_limit_w = profile_power_limit_w(&payload);

    let mut voltage_bins: Vec<i64> = plan.iter().map(|p| p.voltage_mv).collect();
    voltage_bins.sort_unstable();
    voltage_bins.dedup();
    let tail_point_count = plan
        .iter()
        .filter(|p| p.voltage_mv >= candidate_voltage_mv)
        .count() as i64;

    let mut flatten_target = FlattenTarget {
        source: "auto-uv-final".into(),
        lock_clock_mhz,
        lock_voltage_mv: Some(candidate_voltage_mv),
        end_voltage_mv: Some(*voltage_bins.last().unwrap()),
        tail_point_count: Some(tail_point_count),
        ceiling_clock_mhz: None,
        tail_rise_bins: None,
    };
    let ceiling = tail_ceiling_clock_mhz(&plan, lock_clock_mhz, candidate_voltage_mv);
    if ceiling > lock_clock_mhz {
        flatten_target.ceiling_clock_mhz = Some(ceiling);
    }
    let saved_tail_rise_bins = match payload.get("flatten_target") {
        Some(Value::Object(ft)) => ft.get("tail_rise_bins").cloned(),
        _ => payload.get("tail_rise_bins").cloned(),
    };
    if let Some(v) = saved_tail_rise_bins {
        if let Some(bins) = value_int(&v) {
            flatten_target.tail_rise_bins = Some(bins.max(0));
        }
    }

    let assignments = load_profile_tier_assignments();
    let disabled = disabled_profile_ids(&read_tier_assignment_payload());
    let (profile_tier, profile_tier_key) =
        profile_tier_summary_fields(&payload, &assignments, &disabled);

    let profile_id = first_nonempty(
        get_str(&payload, "profile_id"),
        get_str(&resolved_payload, "profile_id"),
    );

    Ok(Some(LoadedCurve {
        path,
        profile_id,
        profile_tier,
        profile_tier_key,
        plan,
        lock_clock_mhz,
        candidate_voltage_mv,
        memory_offset_mhz,
        power_limit_w,
        flatten_target,
    }))
}

/// Clamp a profile memory offset against the driver-reported limit (or the
/// static fallback) at apply time — `apply_auto_uv_profile_memory_offset`'s
/// `profile_memory_offset_mhz(..., limit_mhz=driver_limit)`.
pub fn clamp_memory_offset_to_driver(offset_mhz: i64, driver_range_max: Option<i64>) -> i64 {
    // Python (`driver_memory_offset_limit_mhz`): a reported driver max is
    // `max(0, int(range[1]))` — so a negative max clamps the offset to 0. The
    // static Afterburner cap applies only when NVML exposes no range at all.
    let limit = match driver_range_max {
        Some(max) => max.max(0),
        None => MAX_AFTERBURNER_MEM_OFFSET_MHZ,
    };
    offset_mhz.max(0).min(limit)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clamp_memory_offset_driver_limits() {
        assert_eq!(clamp_memory_offset_to_driver(1500, Some(6000)), 1500);
        assert_eq!(clamp_memory_offset_to_driver(7000, Some(6000)), 6000);
        assert_eq!(clamp_memory_offset_to_driver(-100, Some(6000)), 0);
        // Negative driver max → Python's max(0, int(max)) → applied offset 0
        // (NOT the static 2000 fallback, which the driver would reject).
        assert_eq!(clamp_memory_offset_to_driver(1500, Some(-500)), 0);
        // No driver range at all → static Afterburner cap.
        assert_eq!(clamp_memory_offset_to_driver(2500, None), 2000);
    }

    #[test]
    fn tier_normalization_and_labels() {
        assert_eq!(normalize_profile_tier("perf", ""), "performance");
        assert_eq!(normalize_profile_tier("Balance", ""), "balanced");
        assert_eq!(normalize_profile_tier("weird", "balanced"), "balanced");
        assert_eq!(profile_tier_label("efficiency"), "Efficiency");
        assert_eq!(profile_tier_label("bogus"), "");
    }

    #[test]
    fn safe_profile_id_sanitizes() {
        assert_eq!(safe_profile_id("  a b!c  "), "a-b-c");
        assert_eq!(safe_profile_id("!!!"), "profile");
        assert_eq!(safe_profile_id("895mv-2900mhz"), "895mv-2900mhz");
    }

    #[test]
    fn generated_tier_from_tail_rise_bins() {
        let mut p = Map::new();
        p.insert("tail_rise_bins".into(), Value::from(6));
        assert_eq!(generated_profile_tier(&p), "performance");
        p.insert("tail_rise_bins".into(), Value::from(4));
        assert_eq!(generated_profile_tier(&p), "balanced");
        p.insert("tail_rise_bins".into(), Value::from(0));
        assert_eq!(generated_profile_tier(&p), "efficiency");
    }

    #[test]
    fn iso_epoch_orders_correctly() {
        let a = parse_iso_to_epoch("2026-07-05T14:32:10.123456-07:00").unwrap();
        let b = parse_iso_to_epoch("2026-07-05T14:32:11.000000-07:00").unwrap();
        assert!(b > a);
        // Z suffix parses.
        assert!(parse_iso_to_epoch("2026-07-05T21:32:10Z").is_some());
    }
}
