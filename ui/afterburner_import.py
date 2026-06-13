from __future__ import annotations

from datetime import datetime
from pathlib import Path

from afterburner.import_fan_curve import load_config, write_config
from afterburner.import_vf_curve import (
    load_afterburner_runtime_options,
    persist_afterburner_import,
)
from afterburner.vfcurve import (
    discover_afterburner_vf_sections,
    resolve_afterburner_vf_source,
)
from afterburner.vfcurve_describe import describe_afterburner_flatten_validation
from penguin_burner_paths import (
    default_runtime_config_path,
    discover_afterburner_device_profiles,
    managed_afterburner_root,
    resolve_afterburner_root,
    sync_afterburner_export_tree,
)

from .constants import AFTERBURNER_PROFILE_ID
from .gpu_selection import runtime_gpu_index


def configured_afterburner_root() -> str:
    try:
        options = load_afterburner_runtime_options(default_runtime_config_path())
    except Exception:
        return ""
    return str(options.get("afterburner_root", "")).strip()


def afterburner_profile_entries(afterburner_root: str | Path) -> list[dict]:
    root = resolve_afterburner_root(afterburner_root).expanduser()
    missing = []
    if not (root / "MSIAfterburner.cfg").is_file():
        missing.append("MSIAfterburner.cfg")
    if not (root / "Profiles").is_dir():
        missing.append("Profiles/")
    if missing:
        raise FileNotFoundError(
            "Invalid Afterburner directory: missing "
            + ", ".join(missing)
            + f" under {root}"
        )

    entries = []
    for device_profile in discover_afterburner_device_profiles(root):
        for section in discover_afterburner_vf_sections(device_profile):
            if bool(section.get("is_builtin")):
                continue
            status, importable = afterburner_profile_status(section)
            entries.append(
                {
                    "afterburner_root": str(root),
                    "profile_path": str(Path(device_profile).resolve()),
                    "device_profile_relative_path": relative_profile_path(
                        root,
                        device_profile,
                    ),
                    "device_profile_name": Path(device_profile).name,
                    "section": str(section.get("section", "")),
                    "target": afterburner_profile_target_text(section),
                    "target_voltage_mv": afterburner_profile_target_value(
                        section,
                        "lock_voltage_mv",
                    ),
                    "target_clock_mhz": afterburner_profile_target_value(
                        section,
                        "lock_clock_mhz",
                    ),
                    "curve_points": afterburner_section_curve_points(section),
                    "status": status,
                    "importable": bool(importable),
                }
            )
    return sorted(
        entries,
        key=lambda entry: (
            0 if bool(entry.get("importable")) else 1,
            str(entry.get("device_profile_name", "")).lower(),
            str(entry.get("section", "")).lower(),
        ),
    )


def persist_afterburner_import_selection(entry: dict) -> dict:
    if not bool(entry.get("importable")):
        raise ValueError("selected Afterburner profile is not importable")
    config_path = default_runtime_config_path()
    source_root = resolve_afterburner_root(entry.get("afterburner_root", "")).resolve()
    section = str(entry.get("section", "")).strip()
    if not section:
        raise ValueError("selected Afterburner profile has no section name")
    source_profile_path = Path(str(entry.get("profile_path", ""))).expanduser()
    if not source_profile_path.is_file():
        raise FileNotFoundError(f"Afterburner profile file not found: {source_profile_path}")
    device_profile_relative_path = relative_profile_path(source_root, source_profile_path)
    managed_root = sync_afterburner_export_tree(source_root, managed_afterburner_root())
    runtime_options = load_afterburner_runtime_options(config_path)
    runtime_options["afterburner_root"] = str(managed_root)
    runtime_options["afterburner_profile"] = str(section)
    runtime_options["afterburner_device_profile"] = str(device_profile_relative_path)
    persist_afterburner_import(
        config_path,
        runtime_gpu_index(config_path),
        managed_root,
        device_profile_relative_path,
        section,
        runtime_options=runtime_options,
    )
    return {
        "afterburner_root": str(managed_root),
        "device_profile_relative_path": str(device_profile_relative_path),
        "section": str(section),
        "config_path": str(config_path),
    }


def afterburner_import_profile_summary() -> dict | None:
    config_path = default_runtime_config_path()
    try:
        options = load_afterburner_runtime_options(config_path)
    except Exception:
        return None
    root = str(options.get("afterburner_root", "")).strip()
    section = str(options.get("afterburner_profile", "")).strip()
    device_profile = str(options.get("afterburner_device_profile", "")).strip()
    if not root or not section:
        return None
    try:
        source = resolve_afterburner_vf_source(
            afterburner_root=root,
            section=section,
            device_profile_hint=device_profile or None,
            dangerously_skip_validation=bool(options.get("dangerously_skip_validation")),
        )
    except Exception:
        return None
    section_info = source.get("section_info", {})
    clock, voltage = afterburner_profile_target_pair(section_info)
    label = f"MSI Afterburner {source['section']}"
    if clock is not None and voltage is not None:
        label += f" {clock} MHz {voltage} mV"
    return {
        "profile_id": AFTERBURNER_PROFILE_ID,
        "candidate_id": AFTERBURNER_PROFILE_ID,
        "profile_created_at": path_mtime_iso(config_path),
        "profile_source": "MSI Afterburner",
        "runtime_source": "afterburner",
        "path": "",
        "display_name": label,
        "candidate_voltage_mv": voltage,
        "lock_clock_mhz": clock,
        "avg_core_clock_mhz": None,
        "avg_fps": None,
        "avg_power_w": None,
        "efficiency_fps_per_w": None,
        "final_verified": False,
        "afterburner_root": str(source["afterburner_root"]),
        "afterburner_device_profile": str(source["device_profile_relative_path"]),
        "afterburner_profile": str(source["section"]),
        "curve_points": afterburner_section_curve_points(section_info),
    }


def delete_afterburner_import_config() -> bool:
    config_path = default_runtime_config_path()
    config = load_config(config_path)
    gpu = dict(config.get("gpu", {})) if isinstance(config, dict) else {}
    removed = any(
        str(gpu.get(key, "")).strip()
        for key in ("afterburner_profile", "afterburner_device_profile")
    )
    gpu.pop("afterburner_profile", None)
    gpu.pop("afterburner_device_profile", None)
    updated = dict(config) if isinstance(config, dict) else {}
    if gpu:
        updated["gpu"] = gpu
    else:
        updated.pop("gpu", None)
    write_config(config_path, updated)
    return bool(removed)


def afterburner_profile_status(section: dict) -> tuple[str, bool]:
    if bool(section.get("is_valid_manual_candidate")):
        return "Ready", True
    if not bool(section.get("is_manual_candidate")):
        return "Not Importable: same as Defaults or Startup", False
    validation = section.get("flatten_validation")
    if isinstance(validation, dict):
        reason = str(validation.get("reason", "")).strip()
        if reason:
            return f"Not Importable: {reason}", False
        description = describe_afterburner_flatten_validation(validation)
        if description:
            return f"Not Importable: {description}", False
    return "Not Importable: invalid Afterburner V/F preset", False


def afterburner_profile_target_text(section: dict) -> str:
    clock, voltage = afterburner_profile_target_pair(section)
    return "" if clock is None or voltage is None else f"{clock} MHz {voltage} mV"


def afterburner_profile_target_pair(section: dict) -> tuple[int | None, int | None]:
    target = section.get("flatten_target")
    if not isinstance(target, dict):
        return None, None
    try:
        clock = int(round(float(target["lock_clock_mhz"])))
        voltage = int(round(float(target["lock_voltage_mv"])))
    except (KeyError, TypeError, ValueError):
        return None, None
    return clock, voltage


def afterburner_profile_target_value(section: dict, key: str) -> int | None:
    target = section.get("flatten_target")
    if not isinstance(target, dict):
        return None
    try:
        return int(round(float(target[key])))
    except (KeyError, TypeError, ValueError):
        return None


def afterburner_section_curve_points(section: dict) -> list[tuple[float, float]]:
    materialization = section.get("materialization")
    raw_points = (
        materialization.get("points")
        if isinstance(materialization, dict)
        else section.get("points")
    )
    if not isinstance(raw_points, list):
        return []
    points = []
    for point in raw_points:
        if not isinstance(point, dict):
            continue
        voltage = point.get("voltage_mv")
        clock = point.get("frequency_mhz", point.get("target_mhz"))
        try:
            points.append((float(voltage), float(clock)))
        except (TypeError, ValueError):
            continue
    return points


def entry_curve_points(entry: dict) -> list[tuple[float, float]]:
    raw_points = entry.get("curve_points")
    if not isinstance(raw_points, list):
        return []
    points = []
    for point in raw_points:
        try:
            points.append((float(point[0]), float(point[1])))
        except (IndexError, TypeError, ValueError):
            continue
    return points


def relative_profile_path(root: str | Path, profile_path: str | Path) -> str:
    root_path = Path(root).expanduser().resolve()
    path = Path(profile_path).expanduser().resolve()
    try:
        return str(path.relative_to(root_path))
    except ValueError:
        return path.name


def path_mtime_iso(path: str | Path) -> str:
    try:
        return datetime.fromtimestamp(Path(path).stat().st_mtime).astimezone().isoformat()
    except OSError:
        return ""
