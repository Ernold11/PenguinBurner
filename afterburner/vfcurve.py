#!/usr/bin/env python3

from __future__ import annotations

from collections import Counter
import configparser
import hashlib
import re
import struct
import subprocess
from pathlib import Path

from penguin_burner_paths import (
    afterburner_global_profile,
    default_afterburner_device_profile,
    discover_afterburner_device_profiles,
    resolve_afterburner_root,
)
from subprocess_locale import stable_subprocess_env

DEFAULT_PROFILE = default_afterburner_device_profile()
DEFAULT_SECTION = "startup"
THIRD_VALUE_EPSILON = 1e-6
ADJUSTED_ANCHOR_MIN_DROP_MHZ = 100.0
FLAT_TAIL_MIN_POINTS = 4
FLAT_TAIL_FREQ_EPSILON_MHZ = 1.0
BUILTIN_AFTERBURNER_VF_SECTIONS = {"defaults", "startup"}
MIN_FLATTEN_UNDERVOLT_MARGIN_MV = 5.0

SECTION_TO_LINE = {
    "defaults": 7,
    "startup": 21,
    "profile1": 33,
    "profile2": 45,
    "profile3": 57,
}

DEVICE_PROFILE_NAME_RE = re.compile(
    r"VEN_(?P<vendor>[0-9A-F]{4})&DEV_(?P<device>[0-9A-F]{4}).*?&BUS_(?P<bus>\d+)",
    re.IGNORECASE,
)


class CaseSensitiveRawConfigParser(configparser.RawConfigParser):
    def optionxform(self, optionstr: str) -> str:
        return optionstr


def _parse_afterburner_device_profile_name(path):
    match = DEVICE_PROFILE_NAME_RE.search(Path(path).name)
    if match is None:
        return None
    return {
        "vendor_id": int(match.group("vendor"), 16),
        "device_id": int(match.group("device"), 16),
        "bus_decimal": int(match.group("bus")),
    }


def _detect_active_nvidia_gpus():
    try:
        output = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=pci.device_id,pci.bus_id",
                "--format=csv,noheader",
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            stderr=subprocess.DEVNULL,
            env=stable_subprocess_env(),
        )
    except Exception:
        return []

    detected = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            pci_device_id = int(parts[0], 16)
        except ValueError:
            continue
        bus_segment = parts[1].split(":")
        if len(bus_segment) < 2:
            continue
        try:
            bus_decimal = int(bus_segment[1], 16)
        except ValueError:
            continue
        detected.append(
            {
                "vendor_id": pci_device_id & 0xFFFF,
                "device_id": (pci_device_id >> 16) & 0xFFFF,
                "bus_decimal": bus_decimal,
            }
        )
    return detected


def parse_vfcurve_blob(hex_blob: str):
    data = bytes.fromhex(hex_blob.strip())
    values = [
        struct.unpack_from("<f", data, offset)[0] for offset in range(0, len(data), 4)
    ]

    header = {
        "magic_u32": struct.unpack_from("<I", data, 0)[0],
        "point_count_hint": struct.unpack_from("<I", data, 4)[0],
        "flags_u32": struct.unpack_from("<I", data, 8)[0],
    }

    points = []
    end_index = None
    for index in range(3, len(values) - 2, 3):
        voltage_mv, frequency_mhz, third = values[index : index + 3]
        if voltage_mv == 0.0 and frequency_mhz == 0.0 and third == 0.0:
            end_index = index
            break
        points.append(
            {
                "index": len(points),
                "voltage_mv": float(voltage_mv),
                "frequency_mhz": float(frequency_mhz),
                "third_value": float(third),
            }
        )

    tail = []
    if end_index is not None:
        for index, value in enumerate(values[end_index:], start=end_index):
            if value != 0.0:
                tail.append({"float_index": index, "value": float(value)})

    return header, points, tail


def load_afterburner_vfcurve_hex(profile_path=DEFAULT_PROFILE, section=DEFAULT_SECTION):
    settings = load_afterburner_profile_settings(
        profile_path=profile_path, section=section
    )
    hex_blob = settings["vf_curve_hex"]
    if not hex_blob:
        raise KeyError(
            f"Afterburner section {settings['section']} does not contain a VFCurve blob"
        )
    return hex_blob


def hash_afterburner_vfcurve_hex(hex_blob: str) -> str:
    return hashlib.sha256(hex_blob.strip().encode()).hexdigest()


def load_afterburner_vfcurve(profile_path=DEFAULT_PROFILE, section=DEFAULT_SECTION):
    hex_blob = load_afterburner_vfcurve_hex(profile_path=profile_path, section=section)
    return parse_vfcurve_blob(hex_blob)


def _load_afterburner_profile_parser(profile_path):
    parser = CaseSensitiveRawConfigParser()
    with Path(profile_path).open(
        "r", encoding="utf-8", errors="ignore"
    ) as profile_file:
        parser.read_file(profile_file)
    return parser


def _normalize_afterburner_name(value):
    return str(value).strip().lower()


def _resolve_afterburner_section_name(parser, section):
    normalized = _normalize_afterburner_name(section)
    for candidate in parser.sections():
        if _normalize_afterburner_name(candidate) == normalized:
            return candidate
    raise KeyError(f"Afterburner section not found: {section}")


def _get_afterburner_section_value(values, key, default=""):
    normalized = _normalize_afterburner_name(key)
    for candidate, value in values.items():
        if _normalize_afterburner_name(candidate) == normalized:
            return value
    return default


def _parse_optional_int(value):
    text = str(value).strip()
    if not text:
        return None
    return int(text)


def load_afterburner_profile_section(
    profile_path=DEFAULT_PROFILE, section=DEFAULT_SECTION
):
    parser = _load_afterburner_profile_parser(profile_path)
    resolved_section = _resolve_afterburner_section_name(parser, section)
    values = {key: value.strip() for key, value in parser.items(resolved_section)}
    return resolved_section, values


def load_afterburner_profile_settings(
    profile_path=DEFAULT_PROFILE, section=DEFAULT_SECTION
):
    resolved_section, values = load_afterburner_profile_section(
        profile_path=profile_path,
        section=section,
    )
    return {
        "section": resolved_section,
        "format": _parse_optional_int(_get_afterburner_section_value(values, "Format")),
        "power_limit_pct": _parse_optional_int(
            _get_afterburner_section_value(values, "PowerLimit")
        ),
        "thermal_limit_raw": _get_afterburner_section_value(values, "ThermalLimit"),
        "thermal_prioritize": _parse_optional_int(
            _get_afterburner_section_value(values, "ThermalPrioritize")
        ),
        "core_clk_boost_khz": _parse_optional_int(
            _get_afterburner_section_value(values, "CoreClkBoost")
        ),
        "mem_clk_boost_khz": _parse_optional_int(
            _get_afterburner_section_value(values, "MemClkBoost")
        ),
        "fan_mode": _parse_optional_int(
            _get_afterburner_section_value(values, "FanMode")
        ),
        "fan_speed_pct": _parse_optional_int(
            _get_afterburner_section_value(values, "FanSpeed")
        ),
        "fan_mode2": _parse_optional_int(
            _get_afterburner_section_value(values, "FanMode2")
        ),
        "fan_speed2_pct": _parse_optional_int(
            _get_afterburner_section_value(values, "FanSpeed2")
        ),
        "vf_curve_hex": _get_afterburner_section_value(values, "VFCurve"),
        "raw_values": values,
    }


def point_map_by_voltage(points):
    return {int(round(point["voltage_mv"])): point for point in points}


class AfterburnerVfCurveSafetyError(RuntimeError):
    pass


class AfterburnerProfileSelectionError(RuntimeError):
    pass


def _describe_afterburner_profile_candidates(sections):
    labels = []
    for section in sections:
        label = str(section["section"])
        flatten_target = section.get("flatten_target")
        if flatten_target is not None:
            label += (
                f" ({int(flatten_target['lock_clock_mhz'])}MHz"
                f"@{int(flatten_target['lock_voltage_mv'])}mV)"
            )
        flatten_validation = section.get("flatten_validation")
        if isinstance(flatten_validation, dict):
            margin_mv = flatten_validation.get("undervolt_margin_mv")
            baseline_section = str(
                flatten_validation.get("baseline_section", "")
            ).strip()
            if margin_mv is not None and baseline_section:
                label += f" uv=+{int(round(float(margin_mv)))}mV-vs-{baseline_section}"
        labels.append(label)
    return ", ".join(labels)


def _discover_afterburner_vf_sections_for_profile(profile_path):
    parser = _load_afterburner_profile_parser(profile_path)
    sections = []
    baseline_hashes = {}

    for section_name in parser.sections():
        values = {key: value.strip() for key, value in parser.items(section_name)}
        vf_curve_hex = _get_afterburner_section_value(values, "VFCurve")
        if not vf_curve_hex:
            continue

        header, points, tail = parse_vfcurve_blob(vf_curve_hex)
        analysis = analyze_afterburner_vfcurve(points)
        materialization = materialize_afterburner_vfcurve(points)
        flatten_target = derive_afterburner_dynamic_lock(materialization["points"])
        curve_sha256 = hash_afterburner_vfcurve_hex(vf_curve_hex)
        normalized_name = _normalize_afterburner_name(section_name)

        if normalized_name in BUILTIN_AFTERBURNER_VF_SECTIONS:
            baseline_hashes[normalized_name] = curve_sha256

        sections.append(
            {
                "section": section_name,
                "normalized_section": normalized_name,
                "profile_path": Path(profile_path),
                "curve_sha256": curve_sha256,
                "settings": {
                    "section": section_name,
                    "format": _parse_optional_int(
                        _get_afterburner_section_value(values, "Format")
                    ),
                    "power_limit_pct": _parse_optional_int(
                        _get_afterburner_section_value(values, "PowerLimit")
                    ),
                    "thermal_limit_raw": _get_afterburner_section_value(
                        values, "ThermalLimit"
                    ),
                    "thermal_prioritize": _parse_optional_int(
                        _get_afterburner_section_value(values, "ThermalPrioritize")
                    ),
                    "core_clk_boost_khz": _parse_optional_int(
                        _get_afterburner_section_value(values, "CoreClkBoost")
                    ),
                    "mem_clk_boost_khz": _parse_optional_int(
                        _get_afterburner_section_value(values, "MemClkBoost")
                    ),
                    "fan_mode": _parse_optional_int(
                        _get_afterburner_section_value(values, "FanMode")
                    ),
                    "fan_speed_pct": _parse_optional_int(
                        _get_afterburner_section_value(values, "FanSpeed")
                    ),
                    "fan_mode2": _parse_optional_int(
                        _get_afterburner_section_value(values, "FanMode2")
                    ),
                    "fan_speed2_pct": _parse_optional_int(
                        _get_afterburner_section_value(values, "FanSpeed2")
                    ),
                    "vf_curve_hex": vf_curve_hex,
                    "raw_values": values,
                },
                "header": header,
                "points": points,
                "tail": tail,
                "analysis": analysis,
                "materialization": materialization,
                "flatten_target": flatten_target,
            }
        )

    for section in sections:
        normalized_name = section["normalized_section"]
        curve_sha256 = section["curve_sha256"]
        is_builtin = normalized_name in BUILTIN_AFTERBURNER_VF_SECTIONS
        matches_defaults = curve_sha256 == baseline_hashes.get("defaults")
        matches_startup = curve_sha256 == baseline_hashes.get("startup")
        section["is_builtin"] = is_builtin
        section["matches_defaults"] = matches_defaults
        section["matches_startup"] = matches_startup
        section["is_manual_candidate"] = (
            not is_builtin and not matches_defaults and not matches_startup
        )

    baseline_sections = [
        section for section in sections if section["normalized_section"] == "defaults"
    ]
    baseline_sections.extend(
        section for section in sections if section["normalized_section"] == "startup"
    )
    for section in sections:
        if not section["is_manual_candidate"]:
            continue
        flatten_validation = validate_afterburner_flatten_candidate(
            section,
            baseline_sections=baseline_sections,
        )
        section["flatten_validation"] = flatten_validation
        section["is_valid_manual_candidate"] = bool(flatten_validation.get("valid"))

    return sections


def discover_afterburner_vf_sections(profile_path=DEFAULT_PROFILE):
    return _discover_afterburner_vf_sections_for_profile(Path(profile_path))


def _section_passes_profile_selection_validation(
    section_info,
    *,
    dangerously_skip_validation=False,
):
    if dangerously_skip_validation:
        return bool(section_info.get("is_manual_candidate"))
    return bool(section_info.get("is_valid_manual_candidate"))


def resolve_afterburner_device_profile(
    afterburner_root=None,
    *,
    device_profile_hint=None,
    dangerously_skip_validation=False,
):
    if device_profile_hint:
        candidate = Path(device_profile_hint).expanduser()
        if not candidate.is_absolute():
            candidate = resolve_afterburner_root(afterburner_root) / candidate
        if candidate.exists():
            return candidate.resolve()

    device_profiles = discover_afterburner_device_profiles(afterburner_root)
    if not device_profiles:
        root = resolve_afterburner_root(afterburner_root)
        raise FileNotFoundError(
            f"No Afterburner GPU profile export found under {root / 'Profiles'}"
        )

    if len(device_profiles) == 1:
        return device_profiles[0].resolve()

    detected_gpus = _detect_active_nvidia_gpus()
    matched_profiles = []
    for candidate in device_profiles:
        parsed = _parse_afterburner_device_profile_name(candidate)
        if parsed is None:
            continue
        if any(
            parsed["vendor_id"] == detected["vendor_id"]
            and parsed["device_id"] == detected["device_id"]
            and parsed["bus_decimal"] == detected["bus_decimal"]
            for detected in detected_gpus
        ):
            matched_profiles.append(candidate)
    if len(matched_profiles) == 1:
        return matched_profiles[0].resolve()

    manual_candidate_profiles = []
    for candidate in device_profiles:
        sections = _discover_afterburner_vf_sections_for_profile(candidate)
        if any(
            _section_passes_profile_selection_validation(
                section,
                dangerously_skip_validation=dangerously_skip_validation,
            )
            for section in sections
        ):
            manual_candidate_profiles.append(candidate)

    if len(manual_candidate_profiles) == 1:
        return manual_candidate_profiles[0].resolve()

    rendered = ", ".join(path.name for path in device_profiles)
    raise AfterburnerProfileSelectionError(
        "Multiple Afterburner GPU profile exports found and the device profile could not be "
        f"selected automatically: {rendered}"
    )


def _find_device_profiles_with_section(afterburner_root, requested_section):
    normalized_requested = _normalize_afterburner_name(requested_section)
    matches = []
    for candidate in discover_afterburner_device_profiles(afterburner_root):
        sections = _discover_afterburner_vf_sections_for_profile(candidate)
        selected = next(
            (
                section
                for section in sections
                if section["normalized_section"] == normalized_requested
            ),
            None,
        )
        if selected is not None:
            matches.append(
                {
                    "profile_path": candidate.resolve(),
                    "section_info": selected,
                }
            )
    return matches


def resolve_afterburner_vf_source(
    *,
    afterburner_root=None,
    profile_path=None,
    section=None,
    device_profile_hint=None,
    dangerously_skip_validation=False,
):
    root = resolve_afterburner_root(afterburner_root)
    requested_section = str(section).strip() if section is not None else ""
    requested_section_profile = None
    if profile_path is None and not device_profile_hint and requested_section:
        section_profile_matches = _find_device_profiles_with_section(
            root, requested_section
        )
        if len(section_profile_matches) == 1:
            requested_section_profile = section_profile_matches[0]["profile_path"]
        elif len(section_profile_matches) > 1:
            valid_section_matches = [
                match
                for match in section_profile_matches
                if _section_passes_profile_selection_validation(
                    match["section_info"],
                    dangerously_skip_validation=dangerously_skip_validation,
                )
            ]
            unique_valid_profiles = {
                str(match["profile_path"]): match["profile_path"]
                for match in valid_section_matches
            }
            if len(unique_valid_profiles) == 1:
                requested_section_profile = next(iter(unique_valid_profiles.values()))
            elif len(unique_valid_profiles) > 1:
                rendered = ", ".join(
                    path.name for path in unique_valid_profiles.values()
                )
                raise AfterburnerProfileSelectionError(
                    f"Multiple Afterburner GPU profile exports contain {requested_section}: {rendered}"
                )
    device_profile = (
        Path(profile_path).expanduser().resolve()
        if profile_path is not None
        else (
            requested_section_profile
            if requested_section_profile is not None
            else resolve_afterburner_device_profile(
                root,
                device_profile_hint=device_profile_hint,
                dangerously_skip_validation=dangerously_skip_validation,
            )
        )
    )
    sections = discover_afterburner_vf_sections(device_profile)

    if not sections:
        raise AfterburnerProfileSelectionError(
            f"No Afterburner V/F sections with a saved VFCurve blob were found in {device_profile.name}"
        )

    if requested_section:
        normalized_requested = _normalize_afterburner_name(requested_section)
        selected = next(
            (
                candidate
                for candidate in sections
                if candidate["normalized_section"] == normalized_requested
            ),
            None,
        )
        if selected is None:
            raise AfterburnerProfileSelectionError(
                f"Afterburner profile {requested_section} was not found in {device_profile.name}"
            )
    else:
        manual_candidates = [
            candidate
            for candidate in sections
            if _section_passes_profile_selection_validation(
                candidate,
                dangerously_skip_validation=dangerously_skip_validation,
            )
        ]
        if not manual_candidates:
            invalid_manual_candidates = [
                candidate for candidate in sections if candidate["is_manual_candidate"]
            ]
            if invalid_manual_candidates:
                invalid_preview = "; ".join(
                    f"{candidate['section']}: "
                    f"{describe_afterburner_flatten_validation(candidate.get('flatten_validation'))}"
                    for candidate in invalid_manual_candidates
                )
                guidance = ""
                if not dangerously_skip_validation:
                    guidance = (
                        ". Re-run with --dangerously-skip-validation if you intentionally "
                        "want to import one anyway."
                    )
                raise AfterburnerProfileSelectionError(
                    "Saved Afterburner V/F presets were found, but none contain a valid undervolt "
                    f"flatten target: {invalid_preview}{guidance}"
                )
            raise AfterburnerProfileSelectionError(
                "No saved non-default Afterburner V/F preset was found. "
                "Save a manual voltage curve to a profile slot in Afterburner first."
            )
        if len(manual_candidates) > 1:
            raise AfterburnerProfileSelectionError(
                "Multiple saved Afterburner V/F presets were found. "
                "Specify which profile to import: "
                + _describe_afterburner_profile_candidates(manual_candidates)
            )
        selected = manual_candidates[0]

    if not selected["is_manual_candidate"]:
        raise AfterburnerProfileSelectionError(
            f"Afterburner profile {selected['section']} does not contain a saved non-default "
            "manual V/F preset."
        )

    if not dangerously_skip_validation:
        if selected["flatten_target"] is None:
            raise AfterburnerProfileSelectionError(
                f"Afterburner profile {selected['section']} does not contain a flattened V/F tail. "
                "Save the manual curve with a flat target point before importing it."
            )

        flatten_validation = selected.get("flatten_validation")
        if not flatten_validation or not flatten_validation.get("valid"):
            raise AfterburnerProfileSelectionError(
                f"Afterburner profile {selected['section']} does not contain a valid undervolt "
                f"flatten target: {describe_afterburner_flatten_validation(flatten_validation)}"
            )

    try:
        device_profile_relative_path = str(device_profile.relative_to(root))
    except ValueError:
        device_profile_relative_path = device_profile.name

    return {
        "afterburner_root": root.resolve(),
        "global_profile_path": afterburner_global_profile(root).resolve(),
        "profile_path": device_profile,
        "device_profile_relative_path": device_profile_relative_path,
        "section": str(selected["section"]),
        "section_info": selected,
        "available_sections": sections,
        "dangerously_skip_validation": bool(dangerously_skip_validation),
    }


def _format_curve_float(value: float) -> str:
    if abs(value - round(value)) < THIRD_VALUE_EPSILON:
        return str(int(round(value)))
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _round_curve_float(value: float) -> float:
    return round(float(value), 6)


def detect_afterburner_adjusted_anchor(points):
    if len(points) < 3:
        return None

    third_histogram = Counter(
        _round_curve_float(point["third_value"]) for point in points
    )
    dominant_third_value, dominant_count = third_histogram.most_common(1)[0]
    if dominant_count < len(points) - 1:
        return None

    special_points = [
        point
        for point in points
        if abs(float(point["third_value"]) - dominant_third_value) > THIRD_VALUE_EPSILON
    ]
    if len(special_points) != 1:
        return None

    anchor = special_points[0]
    anchor_index = int(anchor["index"])
    if anchor_index <= 0 or anchor_index >= len(points) - 1:
        return None

    anchor_frequency_mhz = float(anchor["frequency_mhz"])
    prev_frequency_mhz = float(points[anchor_index - 1]["frequency_mhz"])
    next_frequency_mhz = float(points[anchor_index + 1]["frequency_mhz"])
    if anchor_frequency_mhz > (
        min(prev_frequency_mhz, next_frequency_mhz) - ADJUSTED_ANCHOR_MIN_DROP_MHZ
    ):
        return None

    clamp_start = next(
        (
            point
            for point in points
            if float(point["frequency_mhz"])
            > anchor_frequency_mhz + THIRD_VALUE_EPSILON
        ),
        None,
    )
    if clamp_start is None:
        return None

    clamped_point_count = sum(
        1
        for point in points
        if float(point["frequency_mhz"]) > anchor_frequency_mhz + THIRD_VALUE_EPSILON
    )
    if clamped_point_count == 0:
        return None

    return {
        "index": anchor_index,
        "voltage_mv": float(anchor["voltage_mv"]),
        "frequency_mhz": anchor_frequency_mhz,
        "third_value": float(anchor["third_value"]),
        "dominant_third_value": float(dominant_third_value),
        "clamp_start_index": int(clamp_start["index"]),
        "clamp_start_voltage_mv": float(clamp_start["voltage_mv"]),
        "clamp_start_frequency_mhz": float(clamp_start["frequency_mhz"]),
        "clamped_point_count": int(clamped_point_count),
    }


def materialize_afterburner_vfcurve(points):
    analysis = analyze_afterburner_vfcurve(points)
    adjusted_anchor = analysis["adjusted_anchor"]

    if adjusted_anchor is None:
        return {
            "mode": "plain-points"
            if analysis["nonzero_third_value_count"] == 0
            else "raw-points",
            "points": [dict(point) for point in points],
            "adjusted_anchor": None,
        }

    cap_mhz = float(adjusted_anchor["frequency_mhz"])
    effective_points = []
    clamped_point_count = 0
    for point in points:
        raw_frequency_mhz = float(point["frequency_mhz"])
        effective_frequency_mhz = min(raw_frequency_mhz, cap_mhz)
        if effective_frequency_mhz < raw_frequency_mhz - THIRD_VALUE_EPSILON:
            clamped_point_count += 1

        effective_point = dict(point)
        effective_point["raw_frequency_mhz"] = raw_frequency_mhz
        effective_point["frequency_mhz"] = effective_frequency_mhz
        effective_points.append(effective_point)

    adjusted_anchor = dict(adjusted_anchor)
    adjusted_anchor["clamped_point_count"] = int(clamped_point_count)
    return {
        "mode": "adjusted-anchor-cap",
        "points": effective_points,
        "adjusted_anchor": adjusted_anchor,
    }


def detect_afterburner_flat_tail(points):
    if len(points) < FLAT_TAIL_MIN_POINTS:
        return None

    plateau_frequency_mhz = float(points[-1]["frequency_mhz"])
    tail_points = []
    for point in reversed(points):
        if (
            abs(float(point["frequency_mhz"]) - plateau_frequency_mhz)
            > FLAT_TAIL_FREQ_EPSILON_MHZ
        ):
            break
        tail_points.append(point)

    if len(tail_points) < FLAT_TAIL_MIN_POINTS:
        return None

    tail_points.reverse()
    start_point = tail_points[0]
    end_point = tail_points[-1]
    return {
        "frequency_mhz": float(plateau_frequency_mhz),
        "start_voltage_mv": float(start_point["voltage_mv"]),
        "end_voltage_mv": float(end_point["voltage_mv"]),
        "point_count": len(tail_points),
    }


def find_afterburner_point_for_voltage(points, voltage_mv):
    if not points:
        return None

    target_voltage_mv = float(voltage_mv)
    ordered_points = sorted(points, key=lambda point: float(point["voltage_mv"]))
    for point in ordered_points:
        if float(point["voltage_mv"]) + THIRD_VALUE_EPSILON >= target_voltage_mv:
            return point
    return ordered_points[-1]


def find_afterburner_point_for_frequency(points, frequency_mhz):
    if not points:
        return None

    target_frequency_mhz = float(frequency_mhz)
    ordered_points = sorted(
        points,
        key=lambda point: (float(point["frequency_mhz"]), float(point["voltage_mv"])),
    )
    for point in ordered_points:
        if float(point["frequency_mhz"]) + THIRD_VALUE_EPSILON >= target_frequency_mhz:
            return point
    return None


def validate_afterburner_flatten_candidate(section_info, *, baseline_sections):
    flatten_target = section_info.get("flatten_target")
    if flatten_target is None:
        return {
            "valid": False,
            "reason": "no flattened V/F tail was detected",
        }

    lock_voltage_mv = flatten_target.get("lock_voltage_mv")
    lock_clock_mhz = flatten_target.get("lock_clock_mhz")
    if lock_voltage_mv is None or lock_clock_mhz is None:
        return {
            "valid": False,
            "reason": "the flattened V/F target is incomplete",
        }

    selected_point = find_afterburner_point_for_voltage(
        section_info["materialization"]["points"],
        lock_voltage_mv,
    )
    if selected_point is None:
        return {
            "valid": False,
            "reason": "the flattened target could not be matched back to a saved V/F point",
        }

    for baseline_section in baseline_sections:
        if baseline_section["curve_sha256"] == section_info["curve_sha256"]:
            continue

        baseline_point_for_clock = find_afterburner_point_for_frequency(
            baseline_section["materialization"]["points"],
            lock_clock_mhz,
        )
        if baseline_point_for_clock is None:
            continue

        baseline_point_for_voltage = find_afterburner_point_for_voltage(
            baseline_section["materialization"]["points"],
            lock_voltage_mv,
        )
        baseline_required_voltage_mv = float(baseline_point_for_clock["voltage_mv"])
        baseline_same_voltage_clock_mhz = (
            float(baseline_point_for_voltage["frequency_mhz"])
            if baseline_point_for_voltage is not None
            else None
        )
        undervolt_margin_mv = baseline_required_voltage_mv - float(lock_voltage_mv)
        same_voltage_delta_mhz = None
        if baseline_same_voltage_clock_mhz is not None:
            same_voltage_delta_mhz = (
                float(lock_clock_mhz) - baseline_same_voltage_clock_mhz
            )

        valid = undervolt_margin_mv >= MIN_FLATTEN_UNDERVOLT_MARGIN_MV
        reason = ""
        if not valid:
            reason = (
                f"flatten target {int(lock_clock_mhz)}MHz@{int(lock_voltage_mv)}mV is not below "
                f"{baseline_section['section']} at the same clock "
                f"({int(round(baseline_required_voltage_mv))}mV)"
            )

        return {
            "valid": valid,
            "reason": reason,
            "baseline_section": str(baseline_section["section"]),
            "selected_voltage_mv": int(round(float(lock_voltage_mv))),
            "selected_clock_mhz": int(round(float(lock_clock_mhz))),
            "baseline_required_voltage_mv": int(round(baseline_required_voltage_mv)),
            "baseline_same_voltage_clock_mhz": (
                int(round(baseline_same_voltage_clock_mhz))
                if baseline_same_voltage_clock_mhz is not None
                else None
            ),
            "undervolt_margin_mv": float(undervolt_margin_mv),
            "same_voltage_delta_mhz": (
                float(same_voltage_delta_mhz)
                if same_voltage_delta_mhz is not None
                else None
            ),
        }

    return {
        "valid": False,
        "reason": "the flattened target could not be compared against a default Afterburner curve",
    }


def derive_afterburner_dynamic_lock(points):
    flat_tail = detect_afterburner_flat_tail(points)
    if flat_tail is None:
        return None

    return {
        "source": "flat-tail",
        "lock_clock_mhz": int(round(flat_tail["frequency_mhz"])),
        "lock_voltage_mv": int(round(flat_tail["start_voltage_mv"])),
        "end_voltage_mv": int(round(flat_tail["end_voltage_mv"])),
        "tail_point_count": int(flat_tail["point_count"]),
    }


def describe_afterburner_dynamic_lock(dynamic_lock):
    if not dynamic_lock:
        return "disabled"

    parts = [
        f"source={dynamic_lock.get('source', 'unknown')}",
        f"lock={int(dynamic_lock['lock_clock_mhz'])}MHz",
    ]
    lock_voltage_mv = dynamic_lock.get("lock_voltage_mv")
    if lock_voltage_mv is not None:
        parts[-1] += f"@{int(lock_voltage_mv)}mV"

    end_voltage_mv = dynamic_lock.get("end_voltage_mv")
    if end_voltage_mv is not None and lock_voltage_mv is not None:
        parts.append(f"tail={int(lock_voltage_mv)}-{int(end_voltage_mv)}mV")

    tail_point_count = dynamic_lock.get("tail_point_count")
    if tail_point_count:
        parts.append(f"points={int(tail_point_count)}")

    return ", ".join(parts)


def describe_afterburner_flatten_validation(validation):
    if not validation:
        return "unknown"

    if not validation.get("valid"):
        return str(validation.get("reason", "invalid"))

    parts = [
        f"baseline={validation['baseline_section']}",
        (
            f"target={int(validation['selected_clock_mhz'])}MHz"
            f"@{int(validation['selected_voltage_mv'])}mV"
        ),
        f"default-same-clock={int(validation['baseline_required_voltage_mv'])}mV",
        f"uv-margin=+{int(round(float(validation['undervolt_margin_mv'])))}mV",
    ]
    baseline_same_voltage_clock_mhz = validation.get("baseline_same_voltage_clock_mhz")
    same_voltage_delta_mhz = validation.get("same_voltage_delta_mhz")
    if (
        baseline_same_voltage_clock_mhz is not None
        and same_voltage_delta_mhz is not None
    ):
        parts.append(f"default@same-voltage={int(baseline_same_voltage_clock_mhz)}MHz")
        parts.append(
            f"same-voltage-delta={int(round(float(same_voltage_delta_mhz))):+d}MHz"
        )
    return ", ".join(parts)


def analyze_afterburner_vfcurve(points):
    frequencies = [float(point["frequency_mhz"]) for point in points]
    nonzero_third_values = [
        float(point["third_value"])
        for point in points
        if abs(float(point["third_value"])) > THIRD_VALUE_EPSILON
    ]
    unique_nonzero_third_values = sorted(
        {round(value, 6) for value in nonzero_third_values}
    )
    adjusted_anchor = detect_afterburner_adjusted_anchor(points)
    return {
        "point_count": len(points),
        "min_frequency_mhz": min(frequencies) if frequencies else 0.0,
        "max_frequency_mhz": max(frequencies) if frequencies else 0.0,
        "nonzero_third_value_count": len(nonzero_third_values),
        "unique_nonzero_third_values": unique_nonzero_third_values,
        "adjusted_anchor": adjusted_anchor,
    }


def describe_afterburner_vfcurve_analysis(analysis):
    parts = [
        f"points={analysis['point_count']}",
        (
            "freq-range="
            f"{_format_curve_float(analysis['min_frequency_mhz'])}-"
            f"{_format_curve_float(analysis['max_frequency_mhz'])}MHz"
        ),
    ]

    if analysis["nonzero_third_value_count"] == 0:
        parts.append("third-field=all-zero")
    else:
        preview = ", ".join(
            _format_curve_float(value)
            for value in analysis["unique_nonzero_third_values"][:4]
        )
        if len(analysis["unique_nonzero_third_values"]) > 4:
            preview += ", ..."
        parts.append(
            "third-field=nonzero("
            f"{preview}; "
            f"{analysis['nonzero_third_value_count']} point(s))"
        )
        if analysis["adjusted_anchor"] is not None:
            anchor = analysis["adjusted_anchor"]
            parts.append(
                "decoded=adjusted-anchor("
                f"{_format_curve_float(anchor['voltage_mv'])}mV/"
                f"{_format_curve_float(anchor['frequency_mhz'])}MHz, "
                f"clamp-from={_format_curve_float(anchor['clamp_start_voltage_mv'])}mV, "
                f"clamped={anchor['clamped_point_count']})"
            )

    return ", ".join(parts)


def describe_afterburner_profile_settings(settings):
    parts = []

    power_limit_pct = settings.get("power_limit_pct")
    if power_limit_pct is not None:
        parts.append(f"power-limit={int(power_limit_pct)}%")

    core_clk_boost_khz = settings.get("core_clk_boost_khz")
    if core_clk_boost_khz is not None:
        parts.append(f"core-boost={int(core_clk_boost_khz)}kHz")

    mem_clk_boost_khz = settings.get("mem_clk_boost_khz")
    if mem_clk_boost_khz is not None:
        parts.append(f"mem-boost={int(mem_clk_boost_khz)}kHz")

    thermal_limit_raw = str(settings.get("thermal_limit_raw", "")).strip()
    if thermal_limit_raw:
        parts.append(f"thermal-limit={thermal_limit_raw}")

    fan_mode = settings.get("fan_mode")
    fan_speed_pct = settings.get("fan_speed_pct")
    if fan_mode is not None or fan_speed_pct is not None:
        fan_text = "fan1="
        if fan_mode is not None:
            fan_text += f"mode{int(fan_mode)}"
        if fan_speed_pct is not None:
            fan_text += f"@{int(fan_speed_pct)}%"
        parts.append(fan_text)

    fan_mode2 = settings.get("fan_mode2")
    fan_speed2_pct = settings.get("fan_speed2_pct")
    if fan_mode2 is not None or fan_speed2_pct is not None:
        fan_text = "fan2="
        if fan_mode2 is not None:
            fan_text += f"mode{int(fan_mode2)}"
        if fan_speed2_pct is not None:
            fan_text += f"@{int(fan_speed2_pct)}%"
        parts.append(fan_text)

    return ", ".join(parts) if parts else "none"


def ensure_safe_afterburner_vfcurve(points, *, section="startup", allow_unsafe=False):
    analysis = analyze_afterburner_vfcurve(points)
    analysis["section"] = str(section)
    analysis["allow_unsafe"] = bool(allow_unsafe)
    return analysis
