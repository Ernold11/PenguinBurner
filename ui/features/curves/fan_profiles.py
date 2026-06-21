from __future__ import annotations

from pathlib import Path
import json
import math

from curve_editors.fan import user_edited_fan_curve_profile_payload
from profiles.uv import archive_auto_uv_profile
from common.penguin_burner_paths import claim_desktop_user_ownership
from common.penguin_burner_paths import default_user_config_dir


def profile_fan_curve_points(profile: dict) -> list[tuple[float, float]]:
    payload = profile_fan_payload(profile)
    return [] if payload is None else fan_curve_points_from_payload(payload)


def profile_fan_measurement_points(profile: dict) -> list[tuple[float, float]]:
    payload = profile_fan_payload(profile)
    return [] if payload is None else fan_measurement_points_from_payload(payload)


def profile_fan_curve_target_point(profile: dict) -> tuple[float, float] | None:
    payload = profile_fan_payload(profile)
    return None if payload is None else fan_curve_target_point_from_payload(payload)


def profile_fan_payload(profile: dict) -> dict | None:
    for payload in (
        profile,
        profile_payload_from_path(profile),
        matching_current_auto_uv_fan_payload(profile),
    ):
        fan_payload = embedded_fan_payload(payload)
        if fan_payload is not None:
            return fan_payload
    return None


def save_edited_fan_profile(profile: dict, edit, original_points) -> tuple[Path, dict]:
    parent_payload = profile_payload_from_path(profile) or dict(profile)
    fan_payload = profile_fan_payload(profile) or {}
    if not profile_payload_has_saved_vf_curve(parent_payload):
        raise ValueError("selected profile does not contain a saved Auto-UV V/F curve")
    payload = user_edited_fan_curve_profile_payload(
        parent_payload,
        fan_payload,
        edit,
        original_points=original_points,
    )
    path = archive_auto_uv_profile(payload)
    fan_curve_payload = payload.get("fan_curve_payload")
    if isinstance(fan_curve_payload, dict):
        write_auto_uv_fan_curve_payload(fan_curve_payload)
    return path, payload


def sync_profile_fan_payload(profile: dict) -> bool:
    payload = profile_fan_payload(profile)
    if payload is None or not fan_payload_has_silent_runtime_fields(payload):
        return False
    # The runtime service reads this shared artifact when --silent-fan-curve is used.
    write_auto_uv_fan_curve_payload(payload)
    return True


def fan_curve_points_from_payload(payload: dict) -> list[tuple[float, float]]:
    if not isinstance(payload, dict):
        return []
    fan = payload.get("fan")
    if isinstance(fan, dict):
        for key in ("curve", "points"):
            points = fan_curve_points_from_values(fan.get(key))
            if points:
                return points
    for key in ("curve", "points", "fan_points"):
        points = fan_curve_points_from_values(payload.get(key))
        if points:
            return points
    return []


def fan_measurement_points_from_payload(payload: dict) -> list[tuple[float, float]]:
    if not isinstance(payload, dict):
        return []
    telemetry = payload.get("telemetry")
    if isinstance(telemetry, dict):
        for key in ("measured_fan_points", "measured_points", "probe_points"):
            points = fan_measurement_points(telemetry.get(key))
            if points:
                return sorted_unique_fan_points(points)
    for key in ("measured_fan_points", "measured_points", "probe_points"):
        points = fan_measurement_points(payload.get(key))
        if points:
            return sorted_unique_fan_points(points)
    return []


def fan_curve_target_point_from_payload(payload: dict) -> tuple[float, float] | None:
    if not isinstance(payload, dict):
        return None
    for temp_key, speed_key in (
        ("load_anchor_temperature_c", "load_anchor_fan_speed_pct"),
        ("loaded_temperature_c", "observed_fan_speed_pct"),
    ):
        point = fan_point_from_values(payload.get(temp_key), payload.get(speed_key))
        if point is not None:
            return point
    telemetry = payload.get("telemetry")
    final = telemetry.get("final") if isinstance(telemetry, dict) else None
    if isinstance(final, dict):
        for temp_key in ("max_temperature_c", "avg_temperature_c"):
            for speed_key in ("max_fan_speed_pct", "avg_fan_speed_pct"):
                point = fan_point_from_values(final.get(temp_key), final.get(speed_key))
                if point is not None:
                    return point
    points = fan_curve_points_from_payload(payload)
    return points[-1] if points else None


def fan_payload_has_silent_runtime_fields(payload: dict) -> bool:
    if not isinstance(payload, dict):
        return False
    if not fan_curve_points_from_payload(payload):
        return False
    return payload.get("loaded_temperature_c") not in (None, "")


def auto_uv_fan_curve_payload_path() -> Path:
    return default_user_config_dir() / "auto-uv-fan-curve.json"


def write_auto_uv_fan_curve_payload(payload: dict) -> Path:
    if not isinstance(payload, dict):
        raise ValueError("fan curve payload must be an object")
    path = auto_uv_fan_curve_payload_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    claim_desktop_user_ownership(path.parent, include_parents=True)
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(path)
    claim_desktop_user_ownership(path)
    return path


def embedded_fan_payload(payload) -> dict | None:
    if not isinstance(payload, dict):
        return None
    for key in ("fan_curve_payload", "fan_curve", "fan_tuning", "auto_uv_fan_curve"):
        value = payload.get(key)
        if isinstance(value, dict) and fan_curve_points_from_payload(value):
            return dict(value)
    if fan_curve_points_from_payload(payload):
        return dict(payload)
    return None


def matching_current_auto_uv_fan_payload(profile: dict) -> dict | None:
    payload = read_json_file(auto_uv_fan_curve_payload_path())
    if not isinstance(payload, dict) or not fan_payload_matches_profile(payload, profile):
        return None
    return payload


def fan_payload_matches_profile(payload: dict, profile: dict) -> bool:
    try:
        profile_voltage_mv = int(round(float(profile.get("candidate_voltage_mv"))))
        profile_clock_mhz = int(round(float(profile.get("lock_clock_mhz"))))
    except (TypeError, ValueError):
        return False
    telemetry = payload.get("telemetry")
    measured_points = (
        telemetry.get("measured_fan_points")
        if isinstance(telemetry, dict)
        else payload.get("measured_points")
    )
    if not isinstance(measured_points, list):
        return False
    for point in measured_points:
        if not isinstance(point, dict):
            continue
        try:
            voltage_mv = int(round(float(point.get("voltage_mv"))))
            clock_mhz = int(round(float(point.get("clock_mhz"))))
        except (TypeError, ValueError):
            continue
        if voltage_mv == profile_voltage_mv and clock_mhz == profile_clock_mhz:
            return True
    return False


def profile_payload_from_path(profile: dict) -> dict | None:
    path_text = str(profile.get("path", "")).strip()
    if not path_text:
        return None
    return read_json_file(Path(path_text).expanduser())


def read_json_file(path: str | Path) -> dict | None:
    try:
        payload = json.loads(
            Path(path).expanduser().read_text(encoding="utf-8", errors="replace")
        )
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def profile_payload_has_saved_vf_curve(payload: dict) -> bool:
    if not isinstance(payload, dict):
        return False
    return bool(payload.get("plan") or payload.get("curve_points") or payload.get("points"))


def profile_id_from_archive_path(path: str | Path) -> str:
    name = Path(path).name
    prefix = "auto-uv-profile-"
    suffix = ".json"
    if name.startswith(prefix) and name.endswith(suffix):
        return name[len(prefix) : -len(suffix)]
    return Path(path).stem


def fan_curve_points_from_values(values) -> list[tuple[float, float]]:
    if not isinstance(values, list):
        return []
    return sorted_unique_fan_points(
        point
        for value in values
        if (point := fan_point_from_sequence_or_mapping(value)) is not None
    )


def fan_measurement_points(values) -> list[tuple[float, float]]:
    if not isinstance(values, list):
        return []
    points = []
    for value in values:
        if isinstance(value, dict):
            point = fan_measurement_point(value)
        elif isinstance(value, (list, tuple)) and len(value) >= 2:
            point = fan_point_from_values(value[0], value[1])
        else:
            point = None
        if point is not None:
            points.append(point)
    return points


def fan_measurement_point(payload: dict) -> tuple[float, float] | None:
    return fan_point_from_values(
        payload.get("temp_c", payload.get("temperature_c", payload.get("avg_temperature_c"))),
        payload.get("fan_pct", payload.get("fan_speed_pct", payload.get("avg_fan_speed_pct"))),
    )


def fan_point_from_sequence_or_mapping(value) -> tuple[float, float] | None:
    if isinstance(value, dict):
        return fan_point_from_values(
            value.get("temperature_c", value.get("temp_c", value.get("temperature", value.get("x")))),
            value.get("speed_pct", value.get("fan_speed_pct", value.get("fan_pct", value.get("speed", value.get("y"))))),
        )
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return fan_point_from_values(value[0], value[1])
    return None


def sorted_unique_fan_points(points) -> list[tuple[float, float]]:
    unique: dict[tuple[float, float], tuple[float, float]] = {}
    for point in points:
        normalized = fan_point_from_values(point[0], point[1])
        if normalized is None:
            continue
        key = (round(normalized[0], 2), round(normalized[1], 2))
        unique[key] = key
    return sorted(unique.values(), key=lambda point: (point[0], point[1]))


def fan_point_from_values(temp, fan) -> tuple[float, float] | None:
    try:
        temp_value = float(temp)
        fan_value = float(fan)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(temp_value) or not math.isfinite(fan_value):
        return None
    if not (0.0 <= fan_value <= 100.0):
        return None
    return temp_value, fan_value
