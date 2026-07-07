//! Minimal TOML reader + runtime/overlay config loading.
//!
//! The Cargo manifest is frozen (no `toml` crate), so this is a tiny hand-rolled
//! subset parser sufficient for the machine-written `penguin_burner.toml` and
//! `overlay.toml` (sections, scalars, and the multi-line nested fan-curve array).
//! Behavior mirrors `cli/runtime_config_file.py` and `overlay/config.py`.

use std::collections::HashMap;
use std::path::PathBuf;

use crate::paths;

pub const OVERLAY_CONFIG_ENV: &str = "PENGUIN_BURNER_OVERLAY_CONFIG";
pub const DEFAULT_OVERLAY_UPDATE_INTERVAL_S: i64 = 1;
pub const MIN_OVERLAY_UPDATE_INTERVAL_S: i64 = 1;
pub const MAX_OVERLAY_UPDATE_INTERVAL_S: i64 = 10;
pub const DEFAULT_ADAPTIVE_TARGET_FPS: f64 = 60.0;

#[derive(Debug, Clone, PartialEq)]
pub enum TomlValue {
    Bool(bool),
    Int(i64),
    Float(f64),
    Str(String),
    Array(Vec<TomlValue>),
}

impl TomlValue {
    pub fn as_bool(&self) -> Option<bool> {
        match self {
            TomlValue::Bool(b) => Some(*b),
            _ => None,
        }
    }
    pub fn as_f64(&self) -> Option<f64> {
        match self {
            TomlValue::Int(i) => Some(*i as f64),
            TomlValue::Float(f) => Some(*f),
            _ => None,
        }
    }
    pub fn as_i64(&self) -> Option<i64> {
        match self {
            TomlValue::Int(i) => Some(*i),
            TomlValue::Float(f) => Some(*f as i64),
            _ => None,
        }
    }
    pub fn as_str(&self) -> Option<&str> {
        match self {
            TomlValue::Str(s) => Some(s),
            _ => None,
        }
    }
    pub fn as_array(&self) -> Option<&[TomlValue]> {
        match self {
            TomlValue::Array(a) => Some(a),
            _ => None,
        }
    }
}

pub type TomlTable = HashMap<String, TomlValue>;
/// Parsed document: section name (`""` for the root) → key/value table.
pub type TomlDoc = HashMap<String, TomlTable>;

struct Parser {
    chars: Vec<char>,
    pos: usize,
}

impl Parser {
    fn new(text: &str) -> Self {
        Parser {
            chars: strip_comments(text).chars().collect(),
            pos: 0,
        }
    }

    fn peek(&self) -> Option<char> {
        self.chars.get(self.pos).copied()
    }

    fn skip_ws_nl(&mut self) {
        while let Some(c) = self.peek() {
            if c.is_whitespace() {
                self.pos += 1;
            } else {
                break;
            }
        }
    }

    fn skip_inline_ws(&mut self) {
        while let Some(c) = self.peek() {
            if c == ' ' || c == '\t' {
                self.pos += 1;
            } else {
                break;
            }
        }
    }

    fn parse(&mut self) -> TomlDoc {
        let mut doc: TomlDoc = HashMap::new();
        let mut section = String::new();
        loop {
            self.skip_ws_nl();
            match self.peek() {
                None => break,
                Some('[') => {
                    self.pos += 1;
                    let mut name = String::new();
                    while let Some(c) = self.peek() {
                        self.pos += 1;
                        if c == ']' {
                            break;
                        }
                        name.push(c);
                    }
                    section = name.trim().to_string();
                    doc.entry(section.clone()).or_default();
                }
                Some(_) => {
                    let mut key = String::new();
                    while let Some(c) = self.peek() {
                        if c == '=' || c == '\n' {
                            break;
                        }
                        self.pos += 1;
                        key.push(c);
                    }
                    if self.peek() != Some('=') {
                        continue; // malformed line; skip
                    }
                    self.pos += 1; // consume '='
                    let value = self.parse_value();
                    if let Some(value) = value {
                        doc.entry(section.clone())
                            .or_default()
                            .insert(key.trim().to_string(), value);
                    }
                }
            }
        }
        doc
    }

    fn parse_value(&mut self) -> Option<TomlValue> {
        self.skip_inline_ws();
        match self.peek() {
            Some('[') => self.parse_array(),
            Some('"') => self.parse_string().map(TomlValue::Str),
            Some('\'') => self.parse_literal_string().map(TomlValue::Str),
            _ => self.parse_scalar_token(),
        }
    }

    fn parse_array(&mut self) -> Option<TomlValue> {
        self.pos += 1; // consume '['
        let mut items = Vec::new();
        loop {
            self.skip_ws_nl();
            match self.peek() {
                None => break,
                Some(']') => {
                    self.pos += 1;
                    break;
                }
                Some(',') => {
                    self.pos += 1;
                }
                Some(_) => {
                    if let Some(value) = self.parse_value() {
                        items.push(value);
                    } else {
                        break;
                    }
                }
            }
        }
        Some(TomlValue::Array(items))
    }

    fn parse_string(&mut self) -> Option<String> {
        self.pos += 1; // opening quote
        let mut out = String::new();
        while let Some(c) = self.peek() {
            self.pos += 1;
            match c {
                '"' => return Some(out),
                '\\' => {
                    if let Some(next) = self.peek() {
                        self.pos += 1;
                        out.push(match next {
                            'n' => '\n',
                            't' => '\t',
                            'r' => '\r',
                            other => other,
                        });
                    }
                }
                other => out.push(other),
            }
        }
        Some(out)
    }

    fn parse_literal_string(&mut self) -> Option<String> {
        self.pos += 1;
        let mut out = String::new();
        while let Some(c) = self.peek() {
            self.pos += 1;
            if c == '\'' {
                return Some(out);
            }
            out.push(c);
        }
        Some(out)
    }

    fn parse_scalar_token(&mut self) -> Option<TomlValue> {
        let mut token = String::new();
        while let Some(c) = self.peek() {
            if c == ',' || c == ']' || c == '\n' {
                break;
            }
            self.pos += 1;
            token.push(c);
        }
        let token = token.trim();
        if token.is_empty() {
            return None;
        }
        Some(classify_scalar(token))
    }
}

fn classify_scalar(token: &str) -> TomlValue {
    match token {
        "true" => return TomlValue::Bool(true),
        "false" => return TomlValue::Bool(false),
        _ => {}
    }
    if let Ok(i) = token.parse::<i64>() {
        return TomlValue::Int(i);
    }
    if let Ok(f) = token.parse::<f64>() {
        return TomlValue::Float(f);
    }
    TomlValue::Str(token.to_string())
}

/// Drop `#` comments that live outside a basic string (state machine).
fn strip_comments(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut in_string = false;
    let mut escaped = false;
    let mut in_comment = false;
    for c in text.chars() {
        if in_comment {
            if c == '\n' {
                in_comment = false;
                out.push('\n');
            }
            continue;
        }
        if in_string {
            out.push(c);
            if escaped {
                escaped = false;
            } else if c == '\\' {
                escaped = true;
            } else if c == '"' {
                in_string = false;
            }
            continue;
        }
        match c {
            '"' => {
                in_string = true;
                out.push(c);
            }
            '#' => in_comment = true,
            other => out.push(other),
        }
    }
    out
}

pub fn parse_toml(text: &str) -> TomlDoc {
    Parser::new(text).parse()
}

// --- config paths -----------------------------------------------------------

pub fn default_runtime_config_path() -> PathBuf {
    paths::user_config_dir().join("penguin_burner.toml")
}

pub fn default_overlay_config_path() -> PathBuf {
    if let Ok(value) = std::env::var(OVERLAY_CONFIG_ENV) {
        let value = value.trim();
        if !value.is_empty() {
            return PathBuf::from(value);
        }
    }
    paths::user_config_dir().join("overlay.toml")
}

/// Read + parse a TOML file, or an empty doc on any error (parity: missing/bad
/// config falls back to defaults).
pub fn read_toml(path: &std::path::Path) -> TomlDoc {
    match std::fs::read(path) {
        Ok(bytes) => parse_toml(&String::from_utf8_lossy(&bytes)),
        Err(_) => HashMap::new(),
    }
}

/// The GPU section values the engine needs (`enable_persistence_mode`, `index`).
pub struct RuntimeGpuConfig {
    pub index: u32,
    pub enable_persistence_mode: bool,
}

pub fn load_runtime_config_doc() -> TomlDoc {
    read_toml(&default_runtime_config_path())
}

pub fn gpu_config(doc: &TomlDoc) -> RuntimeGpuConfig {
    let section = doc.get("gpu");
    let index = section
        .and_then(|s| s.get("index"))
        .and_then(TomlValue::as_i64)
        .filter(|&i| i >= 0)
        .unwrap_or(0) as u32;
    let enable_persistence_mode = section
        .and_then(|s| s.get("enable_persistence_mode"))
        .and_then(TomlValue::as_bool)
        .unwrap_or(true);
    RuntimeGpuConfig {
        index,
        enable_persistence_mode,
    }
}

// --- adaptive target fps ----------------------------------------------------

pub const MIN_ADAPTIVE_TARGET_FPS: f64 = 1.0;
pub const MAX_ADAPTIVE_TARGET_FPS: f64 = 1000.0;

/// `parse_adaptive_target_fps` (adaptive_target_fps.py:24).
pub fn parse_adaptive_target_fps(value: &str, default: f64) -> f64 {
    let text = value.trim();
    let Ok(fps) = text.parse::<f64>() else {
        return default;
    };
    if !fps.is_finite() {
        return default;
    }
    if !(MIN_ADAPTIVE_TARGET_FPS..=MAX_ADAPTIVE_TARGET_FPS).contains(&fps) {
        return default;
    }
    fps
}

/// `adaptive_target_fps_from_config`: `[adaptive].target_fps` of the runtime TOML.
pub fn adaptive_target_fps_from_config(config_path: &std::path::Path, default: f64) -> f64 {
    let doc = read_toml(config_path);
    let value = doc.get("adaptive").and_then(|s| s.get("target_fps"));
    match value {
        None => default,
        Some(TomlValue::Str(s)) if s.is_empty() => default,
        Some(v) => match v {
            TomlValue::Int(i) => parse_adaptive_target_fps(&i.to_string(), default),
            TomlValue::Float(f) => parse_adaptive_target_fps(&format!("{f}"), default),
            TomlValue::Str(s) => parse_adaptive_target_fps(s, default),
            _ => default,
        },
    }
}

/// `adaptive_target_fps_from_env` for the daemon (env=None) path: env primary
/// then alias, else config, else default.
pub fn adaptive_target_fps_from_env() -> f64 {
    for name in [
        "PENGUIN_BURNER_ADAPTIVE_TARGET_FPS",
        "PB_ADAPTIVE_TARGET_FPS",
    ] {
        if let Ok(raw) = std::env::var(name) {
            let raw = raw.trim();
            if !raw.is_empty() {
                return parse_adaptive_target_fps(raw, DEFAULT_ADAPTIVE_TARGET_FPS);
            }
        }
    }
    adaptive_target_fps_from_config(&default_runtime_config_path(), DEFAULT_ADAPTIVE_TARGET_FPS)
}

pub fn adaptive_target_ms_from_fps(fps: f64) -> f64 {
    1000.0 / parse_adaptive_target_fps(&format!("{fps}"), DEFAULT_ADAPTIVE_TARGET_FPS)
}

/// `format_adaptive_target_fps`.
pub fn format_adaptive_target_fps(fps: f64) -> String {
    let target = parse_adaptive_target_fps(&format!("{fps}"), DEFAULT_ADAPTIVE_TARGET_FPS);
    if target.fract() == 0.0 {
        return format!("{}", target as i64);
    }
    let text = format!("{target:.3}");
    let trimmed = text.trim_end_matches('0').trim_end_matches('.');
    trimmed.to_string()
}

// --- overlay config ---------------------------------------------------------

pub struct OverlayConfig {
    pub enabled: bool,
    pub update_interval_s: i64,
}

fn config_bool(value: Option<&TomlValue>, default: bool) -> bool {
    match value {
        Some(TomlValue::Bool(b)) => *b,
        Some(v) => {
            let text = match v {
                TomlValue::Str(s) => s.trim().to_lowercase(),
                TomlValue::Int(i) => i.to_string(),
                _ => return default,
            };
            match text.as_str() {
                "1" | "true" | "yes" | "on" => true,
                "0" | "false" | "no" | "off" => false,
                _ => default,
            }
        }
        None => default,
    }
}

fn clamp_overlay_interval(value: Option<&TomlValue>) -> i64 {
    let parsed = value
        .and_then(TomlValue::as_f64)
        .map_or(DEFAULT_OVERLAY_UPDATE_INTERVAL_S, |f| {
            crate::gpu::round_half_even(f) as i64
        });
    parsed.clamp(MIN_OVERLAY_UPDATE_INTERVAL_S, MAX_OVERLAY_UPDATE_INTERVAL_S)
}

pub fn load_overlay_config(path: &std::path::Path) -> OverlayConfig {
    let doc = read_toml(path);
    let root = doc.get("");
    OverlayConfig {
        enabled: config_bool(root.and_then(|t| t.get("enabled")), false),
        update_interval_s: clamp_overlay_interval(root.and_then(|t| t.get("update_interval_s"))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_sections_scalars_and_nested_arrays() {
        let text = r#"
# comment
[gpu]
index = 0
enable_persistence_mode = true

[fan]
poll_interval_s = 2
hysteresis_c = 2.0
mode = "linear"
curve = [
  [55, 30],
  [65, 35],  # inline comment
  [80, 45],
]

[adaptive]
target_fps = 90
"#;
        let doc = parse_toml(text);
        assert_eq!(doc["gpu"]["index"], TomlValue::Int(0));
        assert_eq!(doc["gpu"]["enable_persistence_mode"], TomlValue::Bool(true));
        assert_eq!(doc["fan"]["poll_interval_s"], TomlValue::Int(2));
        assert_eq!(doc["fan"]["hysteresis_c"], TomlValue::Float(2.0));
        assert_eq!(doc["fan"]["mode"], TomlValue::Str("linear".into()));
        let curve = doc["fan"]["curve"].as_array().unwrap();
        assert_eq!(curve.len(), 3);
        let first = curve[0].as_array().unwrap();
        assert_eq!(first[0], TomlValue::Int(55));
        assert_eq!(first[1], TomlValue::Int(30));
        assert_eq!(doc["adaptive"]["target_fps"], TomlValue::Int(90));
    }

    #[test]
    fn overlay_root_keys() {
        let text = "version = 1\nenabled = true\nupdate_interval_s = 3\n";
        let doc = parse_toml(text);
        assert_eq!(doc[""]["enabled"], TomlValue::Bool(true));
        assert_eq!(doc[""]["update_interval_s"], TomlValue::Int(3));
    }

    #[test]
    fn target_fps_parse_bounds() {
        assert_eq!(parse_adaptive_target_fps("90", 60.0), 90.0);
        assert_eq!(parse_adaptive_target_fps("", 60.0), 60.0);
        assert_eq!(parse_adaptive_target_fps("0", 60.0), 60.0); // below min
        assert_eq!(parse_adaptive_target_fps("2000", 60.0), 60.0); // above max
        assert_eq!(parse_adaptive_target_fps("nan", 60.0), 60.0);
    }

    #[test]
    fn format_fps_integers_and_fractions() {
        assert_eq!(format_adaptive_target_fps(60.0), "60");
        assert_eq!(format_adaptive_target_fps(59.94), "59.94");
    }

    #[test]
    fn overlay_interval_clamps() {
        assert_eq!(clamp_overlay_interval(Some(&TomlValue::Int(0))), 1);
        assert_eq!(clamp_overlay_interval(Some(&TomlValue::Int(50))), 10);
        assert_eq!(clamp_overlay_interval(Some(&TomlValue::Int(3))), 3);
        assert_eq!(clamp_overlay_interval(None), 1);
    }
}
