from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path

from penguin_burner_paths import default_user_config_dir


class LactExportError(RuntimeError):
    pass


def _read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except FileNotFoundError as exc:
        raise LactExportError(f"missing Auto-UV artifact: {path}") from exc
    except json.JSONDecodeError as exc:
        raise LactExportError(f"invalid JSON in Auto-UV artifact: {path}") from exc
    if not isinstance(payload, dict):
        raise LactExportError(f"Auto-UV artifact is not a JSON object: {path}")
    return payload


def _yaml_scalar(value: object) -> str:
    text = str(value)
    if not text:
        return '""'
    safe_chars = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-:/")
    if all(char in safe_chars for char in text):
        return text
    return json.dumps(text)


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(float(lower), min(float(value), float(upper)))


def _fan_curve_yaml(fan_payload: dict | None) -> tuple[list[str], list[str]]:
    if fan_payload is None or fan_payload.get("fan_curve_blocked"):
        return ["    fan_control_enabled: false"], []

    fan = fan_payload.get("fan")
    if not isinstance(fan, dict):
        raise LactExportError("Auto-UV fan curve is missing the fan section")
    raw_curve = fan.get("curve")
    if not isinstance(raw_curve, list) or not raw_curve:
        raise LactExportError("Auto-UV fan curve has no curve points")

    curve: dict[int, float] = {}
    for point in raw_curve:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise LactExportError(f"invalid Auto-UV fan curve point: {point!r}")
        try:
            temp_c = int(round(float(point[0])))
            speed_fraction = _clamp(float(point[1]) / 100.0, 0.0, 1.0)
        except (TypeError, ValueError) as exc:
            raise LactExportError(f"invalid Auto-UV fan curve point: {point!r}") from exc
        curve[temp_c] = round(speed_fraction, 4)

    auto_threshold = fan_payload.get("zero_rpm_until_temperature_c")
    if auto_threshold is None:
        auto_threshold = fan.get("auto_restore_temp_c")
    try:
        auto_threshold_c = int(round(float(auto_threshold or 0)))
    except (TypeError, ValueError):
        auto_threshold_c = 0

    lines = [
        "    fan_control_enabled: true",
        "    fan_control_settings:",
        "      mode: curve",
        "      static_speed: 1.0",
        "      temperature_key: edge",
        f"      interval_ms: {int(float(fan.get('poll_interval_s', 1.0)) * 1000)}",
        "      curve:",
    ]
    for temp_c in sorted(curve):
        lines.append(f"        {temp_c}: {curve[temp_c]:.4g}")
    lines.extend(
        [
            "      spindown_delay_ms: 0",
            f"      change_threshold: {int(round(float(fan.get('hysteresis_c', 0.0))))}",
            f"      auto_threshold: {auto_threshold_c}",
        ]
    )
    warnings: list[str] = []
    if auto_threshold_c <= 0:
        warnings.append("fan auto_threshold is disabled because no zero-RPM temp was saved")
    return lines, warnings


def _vf_curve_yaml(final_curve_payload: dict) -> list[str]:
    raw_points = final_curve_payload.get("points")
    if not isinstance(raw_points, list) or not raw_points:
        raise LactExportError("Auto-UV final curve has no V/F points")

    points: list[tuple[int, int, int]] = []
    for raw in raw_points:
        if not isinstance(raw, dict):
            raise LactExportError(f"invalid Auto-UV V/F point: {raw!r}")
        try:
            index = int(raw["index"])
            voltage_mv = int(raw["voltage_mv"])
            target_mhz = int(raw["target_mhz"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LactExportError(f"invalid Auto-UV V/F point: {raw!r}") from exc
        if not 0 <= index <= 255:
            raise LactExportError(f"V/F point index is outside LACT range: {index}")
        points.append((index, voltage_mv, target_mhz))

    lines = ["    gpu_vf_curve:"]
    for index, voltage_mv, target_mhz in sorted(points):
        lines.extend(
            [
                f"      {index}:",
                f"        clockspeed: {target_mhz}",
                f"        voltage: {voltage_mv}",
            ]
        )
    return lines


def build_lact_nvidia_config(
    *,
    gpu_id: str,
    final_curve_path: Path | None = None,
    fan_curve_path: Path | None = None,
) -> tuple[str, list[str]]:
    gpu_id = str(gpu_id).strip()
    if not gpu_id:
        raise LactExportError("LACT GPU id is required; use `lact cli list-gpus`")

    config_dir = default_user_config_dir()
    final_curve_path = final_curve_path or config_dir / "auto-uv-final-curve.json"
    fan_curve_path = fan_curve_path or config_dir / "auto-uv-fan-curve.json"
    final_curve_payload = _read_json(Path(final_curve_path))
    fan_curve_payload = (
        _read_json(Path(fan_curve_path)) if Path(fan_curve_path).is_file() else None
    )

    fan_lines, warnings = _fan_curve_yaml(fan_curve_payload)
    vf_lines = _vf_curve_yaml(final_curve_payload)
    generated_at = datetime.now().astimezone().isoformat()

    lines = [
        "# Generated by PenguinBurner for LACT Nvidia control.",
        f"# Generated at: {generated_at}",
        f"# Source V/F curve: {final_curve_path}",
        f"# Source fan curve: {fan_curve_path}",
        "# Apply deliberately, for example: sudo install -m 0644 lact-config.yaml /etc/lact/config.yaml",
        "daemon:",
        "  log_level: info",
        "  disable_nvapi: false",
        "apply_settings_timer: 5",
        "gpus:",
        f"  {_yaml_scalar(gpu_id)}:",
    ]
    lines.extend(fan_lines)
    lines.extend(vf_lines)
    lines.extend(["profiles: {}", "current_profile: null", "auto_switch_profiles: false"])
    return "\n".join(lines) + "\n", warnings


def write_lact_nvidia_config(*, output_path: Path, gpu_id: str) -> tuple[Path, list[str]]:
    rendered, warnings = build_lact_nvidia_config(gpu_id=gpu_id)
    output_path = Path(output_path).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered, encoding="utf-8")
    return output_path, warnings
