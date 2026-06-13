from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path

from afterburner.fan_curve import (
    load_afterburner_fan_settings,
    resolve_afterburner_fan_profile,
)
from afterburner.import_fan_curve import build_imported_fan_section
from afterburner.import_vf_curve import build_plan
from afterburner.vfcurve import resolve_afterburner_vf_source
from nvidia_driver.hidden_nvapi_vf import create_hidden_vf_curve_reader
from saved_uv_profiles import resolve_auto_uv_profile
from common.penguin_burner_paths import claim_desktop_user_ownership, default_user_config_dir


class LactExportError(RuntimeError):
    pass


LACT_NVIDIA_DEFAULT_MAX_VF_OFFSET_MHZ = 1000


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


def _fan_config_yaml(fan: dict | None) -> tuple[list[str], list[str]]:
    if fan is None:
        return ["    fan_control_enabled: false"], []

    raw_curve = fan.get("curve")
    if not isinstance(raw_curve, list) or not raw_curve:
        raise LactExportError("fan curve has no curve points")

    curve: dict[int, float] = {}
    for point in raw_curve:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise LactExportError(f"invalid fan curve point: {point!r}")
        try:
            temp_c = int(round(float(point[0])))
            speed_fraction = _clamp(float(point[1]) / 100.0, 0.0, 1.0)
        except (TypeError, ValueError) as exc:
            raise LactExportError(f"invalid fan curve point: {point!r}") from exc
        curve[temp_c] = round(speed_fraction, 4)

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


def _auto_uv_fan_config(fan_payload: dict | None) -> dict | None:
    if fan_payload is None or fan_payload.get("fan_curve_blocked"):
        return None
    fan = fan_payload.get("fan")
    if not isinstance(fan, dict):
        raise LactExportError("Auto-UV fan curve is missing the fan section")
    if fan_payload.get("zero_rpm_until_temperature_c") is not None:
        fan = dict(fan)
        fan["auto_restore_temp_c"] = fan_payload.get("zero_rpm_until_temperature_c")
    return fan


def _optional_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _vf_curve_yaml_from_points(
    raw_points: list[dict],
    *,
    max_vf_offset_mhz: int | None = LACT_NVIDIA_DEFAULT_MAX_VF_OFFSET_MHZ,
) -> tuple[list[str], list[str]]:
    if not isinstance(raw_points, list) or not raw_points:
        raise LactExportError("V/F curve has no points")

    points: list[tuple[int, int, int]] = []
    warnings: list[str] = []
    clamped_offsets: list[str] = []
    clamped_negative: list[str] = []
    max_offset = None if max_vf_offset_mhz is None else max(0, int(max_vf_offset_mhz))
    for raw in raw_points:
        if not isinstance(raw, dict):
            raise LactExportError(f"invalid V/F point: {raw!r}")
        try:
            index = int(raw["index"])
            voltage_mv = int(raw["voltage_mv"])
            target_mhz = int(raw["target_mhz"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LactExportError(f"invalid V/F point: {raw!r}") from exc
        if not 0 <= index <= 255:
            raise LactExportError(f"V/F point index is outside LACT range: {index}")
        base_mhz = _optional_int(raw.get("base_mhz"))
        if base_mhz is not None:
            offset_mhz = int(target_mhz) - int(base_mhz)
            if max_offset is not None and offset_mhz > max_offset:
                target_mhz = int(base_mhz) + int(max_offset)
                clamped_offsets.append(
                    f"index={index} voltage={voltage_mv}mV "
                    f"offset={offset_mhz:+d}->{int(max_offset):+d}MHz"
                )
        if target_mhz < 0:
            clamped_negative.append(
                f"index={index} voltage={voltage_mv}mV clockspeed={target_mhz}->0MHz"
            )
            target_mhz = 0
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
    if clamped_offsets:
        warnings.append(
            "LACT V/F offsets were clamped to "
            f"+{int(max_offset)}MHz over each point's base clock: "
            + "; ".join(clamped_offsets[:8])
            + (" ..." if len(clamped_offsets) > 8 else "")
        )
    if clamped_negative:
        warnings.append(
            "LACT V/F clocks were clamped to non-negative values: "
            + "; ".join(clamped_negative[:8])
            + (" ..." if len(clamped_negative) > 8 else "")
        )
    return lines, warnings


def build_lact_nvidia_config_from_plan(
    *,
    gpu_id: str,
    vf_plan: list[dict] | None,
    fan_config: dict | None = None,
    source_label: str,
    source_vf: str,
    source_fan: str | None = None,
    include_vf_curve: bool = True,
    include_fan_curve: bool = False,
    max_vf_offset_mhz: int | None = LACT_NVIDIA_DEFAULT_MAX_VF_OFFSET_MHZ,
) -> tuple[str, list[str]]:
    gpu_id = str(gpu_id).strip()
    if not gpu_id:
        raise LactExportError("LACT GPU id is required; use `lact cli list-gpus`")

    fan_lines, warnings = _fan_config_yaml(fan_config if include_fan_curve else None)
    if include_vf_curve:
        vf_lines, vf_warnings = _vf_curve_yaml_from_points(
            vf_plan or [],
            max_vf_offset_mhz=max_vf_offset_mhz,
        )
        warnings.extend(vf_warnings)
    else:
        vf_lines = []
    generated_at = datetime.now().astimezone().isoformat()

    lines = [
        "# Generated by PenguinBurner for LACT Nvidia control.",
        f"# Generated at: {generated_at}",
        f"# Source: {source_label}",
        f"# Source V/F curve: {source_vf}",
        f"# Source fan curve: {source_fan or 'none'}",
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


def build_lact_nvidia_config(
    *,
    gpu_id: str,
    final_curve_path: Path | None = None,
    fan_curve_path: Path | None = None,
    profile_selector: str = "",
    include_vf_curve: bool = True,
    include_fan_curve: bool = False,
    max_vf_offset_mhz: int | None = LACT_NVIDIA_DEFAULT_MAX_VF_OFFSET_MHZ,
) -> tuple[str, list[str]]:
    config_dir = default_user_config_dir()
    if final_curve_path is None and include_vf_curve:
        selector = str(profile_selector or "latest").strip() or "latest"
        resolved_profile = resolve_auto_uv_profile(selector)
        final_curve_path = (
            resolved_profile[0]
            if resolved_profile is not None
            else config_dir / "auto-uv-profiles" / "latest-profile.json"
        )
        if resolved_profile is None and selector not in {"active", "latest"}:
            raise LactExportError(f"Auto-UV profile not found: {selector}")
    fan_curve_path = fan_curve_path or config_dir / "auto-uv-fan-curve.json"
    final_curve_payload = _read_json(Path(final_curve_path)) if include_vf_curve else {}
    fan_curve_payload = None
    if include_fan_curve and Path(fan_curve_path).is_file():
        fan_curve_payload = _read_json(Path(fan_curve_path))
    raw_points = final_curve_payload.get("points")
    return build_lact_nvidia_config_from_plan(
        gpu_id=gpu_id,
        vf_plan=raw_points,
        fan_config=_auto_uv_fan_config(fan_curve_payload),
        source_label="auto-uv",
        source_vf=str(final_curve_path) if include_vf_curve else "omitted",
        source_fan=str(fan_curve_path),
        include_vf_curve=include_vf_curve,
        include_fan_curve=include_fan_curve,
        max_vf_offset_mhz=max_vf_offset_mhz,
    )


def write_lact_nvidia_config(
    *,
    output_path: Path,
    gpu_id: str,
    final_curve_path: Path | None = None,
    fan_curve_path: Path | None = None,
    profile_selector: str = "",
    include_vf_curve: bool = True,
    include_fan_curve: bool = False,
    max_vf_offset_mhz: int | None = LACT_NVIDIA_DEFAULT_MAX_VF_OFFSET_MHZ,
) -> tuple[Path, list[str]]:
    rendered, warnings = build_lact_nvidia_config(
        gpu_id=gpu_id,
        final_curve_path=final_curve_path,
        fan_curve_path=fan_curve_path,
        profile_selector=profile_selector,
        include_vf_curve=include_vf_curve,
        include_fan_curve=include_fan_curve,
        max_vf_offset_mhz=max_vf_offset_mhz,
    )
    output_path = Path(output_path).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    claim_desktop_user_ownership(output_path.parent, include_parents=True)
    output_path.write_text(rendered, encoding="utf-8")
    claim_desktop_user_ownership(output_path)
    return output_path, warnings


def _afterburner_fan_config(
    *,
    current_fan_config: dict,
    afterburner_root: str,
    gpu_index: int,
) -> tuple[dict | None, str, list[str]]:
    fan_profile = resolve_afterburner_fan_profile(afterburner_root=afterburner_root)
    warnings: list[str] = []
    try:
        settings = load_afterburner_fan_settings(fan_profile)
        settings["afterburner_root"] = Path(afterburner_root).expanduser()
        if not settings["sw_auto_enabled"]:
            raise LactExportError(
                "Afterburner software auto fan control is disabled in the imported profile"
            )
        fan_config = build_imported_fan_section(
            current_fan_config,
            settings,
            gpu_index=gpu_index,
        )
    except (Exception, SystemExit) as exc:
        fan_config = None
        warnings.append(f"Afterburner fan curve was not exported: {exc}")
    return fan_config, str(fan_profile), warnings


def build_lact_nvidia_config_from_afterburner(
    *,
    gpu_id: str,
    current_fan_config: dict,
    gpu_index: int,
    afterburner_root: str,
    section: str | None = None,
    device_profile_hint: str | None = None,
    dangerously_skip_validation: bool = False,
    preserve_base_below_mv: int | None = None,
    include_vf_curve: bool = True,
    include_fan_curve: bool = False,
    max_vf_offset_mhz: int | None = LACT_NVIDIA_DEFAULT_MAX_VF_OFFSET_MHZ,
) -> tuple[str, list[str]]:
    afterburner_root = str(afterburner_root).strip()
    if not afterburner_root:
        raise LactExportError(
            "--lact-source afterburner requires --afterburner-dir or a configured Afterburner root"
        )

    warnings: list[str] = []
    fan_config = None
    source_fan = None
    if include_fan_curve:
        fan_config, source_fan, warnings = _afterburner_fan_config(
            current_fan_config=current_fan_config,
            afterburner_root=afterburner_root,
            gpu_index=gpu_index,
        )

    vf_plan = None
    source_vf = "omitted"
    if include_vf_curve:
        source = resolve_afterburner_vf_source(
            afterburner_root=afterburner_root,
            section=section or None,
            device_profile_hint=device_profile_hint or None,
            dangerously_skip_validation=bool(dangerously_skip_validation),
        )
        reader = create_hidden_vf_curve_reader(gpu_index=gpu_index)
        if reader is None:
            raise LactExportError("could not open the live Nvidia V/F curve reader")
        try:
            vf_plan, missing_voltage_bins = build_plan(
                reader,
                source["section_info"]["materialization"]["points"],
                preserve_base_below_mv=preserve_base_below_mv,
            )
        finally:
            reader.close()
        if missing_voltage_bins:
            raise LactExportError(
                "Afterburner profile did not cover Linux voltage bins: "
                + ", ".join(str(item) for item in missing_voltage_bins[:16])
                + (" ..." if len(missing_voltage_bins) > 16 else "")
            )
        source_vf = f"{source['profile_path']} [{source['section']}]"

    rendered, render_warnings = build_lact_nvidia_config_from_plan(
        gpu_id=gpu_id,
        vf_plan=vf_plan,
        fan_config=fan_config,
        source_label="afterburner",
        source_vf=source_vf,
        source_fan=source_fan,
        include_vf_curve=include_vf_curve,
        include_fan_curve=include_fan_curve,
        max_vf_offset_mhz=max_vf_offset_mhz,
    )
    warnings.extend(render_warnings)
    return rendered, warnings


def write_lact_nvidia_config_from_afterburner(
    *,
    output_path: Path,
    gpu_id: str,
    current_fan_config: dict,
    gpu_index: int,
    afterburner_root: str,
    section: str | None = None,
    device_profile_hint: str | None = None,
    dangerously_skip_validation: bool = False,
    preserve_base_below_mv: int | None = None,
    include_vf_curve: bool = True,
    include_fan_curve: bool = False,
    max_vf_offset_mhz: int | None = LACT_NVIDIA_DEFAULT_MAX_VF_OFFSET_MHZ,
) -> tuple[Path, list[str]]:
    rendered, warnings = build_lact_nvidia_config_from_afterburner(
        gpu_id=gpu_id,
        current_fan_config=current_fan_config,
        gpu_index=gpu_index,
        afterburner_root=afterburner_root,
        section=section,
        device_profile_hint=device_profile_hint,
        dangerously_skip_validation=dangerously_skip_validation,
        preserve_base_below_mv=preserve_base_below_mv,
        include_vf_curve=include_vf_curve,
        include_fan_curve=include_fan_curve,
        max_vf_offset_mhz=max_vf_offset_mhz,
    )
    output_path = Path(output_path).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    claim_desktop_user_ownership(output_path.parent, include_parents=True)
    output_path.write_text(rendered, encoding="utf-8")
    claim_desktop_user_ownership(output_path)
    return output_path, warnings
