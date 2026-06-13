#!/usr/bin/env python3

import hashlib
import os
import platform
import pwd
import shlex
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

from afterburner.fan_curve import (
    load_afterburner_fan_settings,
    parse_sw_auto_fan_curve,
    resolve_afterburner_fan_profile,
)
from afterburner.vfcurve import (
    discover_afterburner_vf_sections,
    hash_afterburner_vfcurve_hex,
    load_afterburner_profile_section,
    load_afterburner_profile_settings,
    parse_vfcurve_blob,
)
from afterburner.vfcurve_describe import (
    describe_afterburner_dynamic_lock,
    describe_afterburner_flatten_validation,
    describe_afterburner_profile_settings,
    describe_afterburner_vfcurve_analysis,
)
from common.penguin_burner_paths import (
    afterburner_global_profile,
    afterburner_profiles_dir,
    discover_afterburner_device_profiles,
    resolve_afterburner_root,
    validate_afterburner_export_root,
)
from common.subprocess_locale import stable_subprocess_env

NVIDIA_SMI = shutil.which("nvidia-smi") or "nvidia-smi"
DEBUG_LOG_ENABLED = False
DEBUG_LOG_PATH = None
DEBUG_LOG_FILE = None
DEBUG_LOG_MAX_BYTES = 700 * 1024
DEBUG_LOG_BYTES_WRITTEN = 0
DEBUG_LOG_TRUNCATED = False
STDIO_CAPTURE_PATH = None
STDIO_CAPTURE_FILE = None
STDIO_CAPTURE_ORIGINAL_STDOUT = None
STDIO_CAPTURE_ORIGINAL_STDERR = None


def log(message):
    text = str(message)
    print(text, flush=True)
    _write_debug_log_line(text)


def debug_log(message):
    if not DEBUG_LOG_ENABLED:
        return
    text = f"[debug] {message}"
    _write_debug_log_line(text)


def close_debug_log():
    global DEBUG_LOG_FILE
    if DEBUG_LOG_FILE is None:
        return
    DEBUG_LOG_FILE.close()
    DEBUG_LOG_FILE = None


class _TeeStream:
    def __init__(self, original, capture_file):
        self._original = original
        self._capture_file = capture_file

    def write(self, text):
        written = self._original.write(text)
        try:
            self._capture_file.write(str(text))
        except Exception:
            pass
        return written

    def flush(self):
        self._original.flush()
        try:
            self._capture_file.flush()
        except Exception:
            pass

    def isatty(self):
        return self._original.isatty()

    @property
    def encoding(self):
        return getattr(self._original, "encoding", "utf-8")

    @property
    def errors(self):
        return getattr(self._original, "errors", "replace")

    def __getattr__(self, name):
        return getattr(self._original, name)


def close_stdio_capture():
    global \
        STDIO_CAPTURE_FILE, \
        STDIO_CAPTURE_ORIGINAL_STDOUT, \
        STDIO_CAPTURE_ORIGINAL_STDERR
    if STDIO_CAPTURE_FILE is None:
        return
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    if STDIO_CAPTURE_ORIGINAL_STDOUT is not None:
        sys.stdout = STDIO_CAPTURE_ORIGINAL_STDOUT
    if STDIO_CAPTURE_ORIGINAL_STDERR is not None:
        sys.stderr = STDIO_CAPTURE_ORIGINAL_STDERR
    try:
        STDIO_CAPTURE_FILE.close()
    finally:
        STDIO_CAPTURE_FILE = None


def enable_stdio_capture(config_path, *, argv=None, label="stdout"):
    global \
        STDIO_CAPTURE_PATH, \
        STDIO_CAPTURE_FILE, \
        STDIO_CAPTURE_ORIGINAL_STDOUT, \
        STDIO_CAPTURE_ORIGINAL_STDERR
    if STDIO_CAPTURE_FILE is not None:
        return STDIO_CAPTURE_PATH

    config_path = Path(config_path).expanduser().resolve()
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    safe_label = (
        "".join(
            char if char.isalnum() or char in ("-", "_") else "-"
            for char in str(label).strip().lower()
        ).strip("-")
        or "stdout"
    )
    debug_dir = config_path.parent / "debug-logs"
    STDIO_CAPTURE_PATH = debug_dir / f"penguin_burner-{safe_label}-{timestamp}.log"
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
        STDIO_CAPTURE_FILE = STDIO_CAPTURE_PATH.open(
            "a",
            encoding="utf-8",
            errors="replace",
            buffering=1,
        )
    except Exception as exc:
        STDIO_CAPTURE_FILE = None
        print(
            f"warning: failed to open stdout/stderr capture under {debug_dir}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return None
    STDIO_CAPTURE_ORIGINAL_STDOUT = sys.stdout
    STDIO_CAPTURE_ORIGINAL_STDERR = sys.stderr
    sys.stdout = _TeeStream(STDIO_CAPTURE_ORIGINAL_STDOUT, STDIO_CAPTURE_FILE)
    sys.stderr = _TeeStream(STDIO_CAPTURE_ORIGINAL_STDERR, STDIO_CAPTURE_FILE)
    STDIO_CAPTURE_FILE.write("# PenguinBurner captured stdout/stderr log\n")
    STDIO_CAPTURE_FILE.write(f"# started_at={time.strftime('%Y-%m-%d %H:%M:%S %z')}\n")
    STDIO_CAPTURE_FILE.write(
        f"# argv={shlex.join([Path(sys.argv[0]).name, *(argv or [])])}\n"
    )
    STDIO_CAPTURE_FILE.write(f"# cwd={Path.cwd()}\n")
    STDIO_CAPTURE_FILE.write(f"# config-path={config_path}\n")
    STDIO_CAPTURE_FILE.flush()
    return STDIO_CAPTURE_PATH


def _write_debug_log_line(text):
    global DEBUG_LOG_BYTES_WRITTEN, DEBUG_LOG_TRUNCATED
    if DEBUG_LOG_FILE is None or DEBUG_LOG_TRUNCATED:
        return

    line = str(text) + "\n"
    encoded = line.encode("utf-8", errors="replace")
    if DEBUG_LOG_BYTES_WRITTEN + len(encoded) <= DEBUG_LOG_MAX_BYTES:
        DEBUG_LOG_FILE.write(line)
        DEBUG_LOG_FILE.flush()
        DEBUG_LOG_BYTES_WRITTEN += len(encoded)
        return

    remaining = max(0, DEBUG_LOG_MAX_BYTES - DEBUG_LOG_BYTES_WRITTEN)
    truncation_notice = (
        "[debug] debug log truncated after reaching the 700KB safety limit; "
        "foreground and daemon logging continue normally\n"
    )
    encoded_notice = truncation_notice.encode("utf-8", errors="replace")
    if remaining >= len(encoded_notice):
        DEBUG_LOG_FILE.write(truncation_notice)
        DEBUG_LOG_FILE.flush()
        DEBUG_LOG_BYTES_WRITTEN += len(encoded_notice)
    DEBUG_LOG_TRUNCATED = True


def _write_stdio_capture_line(text):
    if STDIO_CAPTURE_FILE is None:
        return
    try:
        STDIO_CAPTURE_FILE.write(str(text) + "\n")
        STDIO_CAPTURE_FILE.flush()
    except Exception:
        pass


def enable_debug_logging(config_path, *, argv=None):
    global \
        DEBUG_LOG_ENABLED, \
        DEBUG_LOG_PATH, \
        DEBUG_LOG_FILE, \
        DEBUG_LOG_BYTES_WRITTEN, \
        DEBUG_LOG_TRUNCATED
    if DEBUG_LOG_ENABLED:
        return DEBUG_LOG_PATH

    DEBUG_LOG_ENABLED = True
    config_path = Path(config_path).expanduser().resolve()
    debug_dir = config_path.parent / "debug-logs"
    debug_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    DEBUG_LOG_PATH = debug_dir / f"penguin_burner-debug-{timestamp}.log"
    try:
        DEBUG_LOG_FILE = DEBUG_LOG_PATH.open("a", encoding="utf-8", buffering=1)
    except Exception as exc:
        DEBUG_LOG_FILE = None
        print(
            f"warning: failed to open debug log file under {debug_dir}: {exc}",
            file=sys.stderr,
            flush=True,
        )
    DEBUG_LOG_BYTES_WRITTEN = 0
    DEBUG_LOG_TRUNCATED = False
    debug_log("debug log enabled")
    if DEBUG_LOG_PATH is not None:
        debug_log(f"debug-log-path={DEBUG_LOG_PATH}")
    debug_log(f"argv={shlex.join([Path(sys.argv[0]).name, *(argv or [])])}")
    debug_log(f"cwd={Path.cwd()}")
    debug_log(f"config-path={config_path}")
    _debug_log_runtime_environment()
    return DEBUG_LOG_PATH


def debug_exception(context, exc):
    if not DEBUG_LOG_ENABLED and STDIO_CAPTURE_FILE is None:
        return

    def _emit(message):
        if DEBUG_LOG_ENABLED:
            debug_log(message)
        if STDIO_CAPTURE_FILE is not None:
            _write_stdio_capture_line(f"[debug] {message}")

    _emit(f"{context}: {exc.__class__.__name__}: {exc}")
    for line in traceback.format_exception(type(exc), exc, exc.__traceback__):
        for fragment in line.rstrip().splitlines():
            _emit(f"traceback: {fragment}")


def _normalized_afterburner_section_name(name):
    return str(name).strip().lower()


def _debug_render_text_preview(value, *, limit=96):
    text = str(value).strip()
    if not text:
        return "<empty>"
    if len(text) <= int(limit):
        return text
    half = max(16, int(limit) // 2 - 3)
    return f"{text[:half]}...{text[-half:]}"


def _debug_render_text_or_full(value, *, full_limit=12288, preview_limit=120):
    text = str(value).strip()
    if len(text) <= int(full_limit):
        return text or "<empty>"
    return _debug_render_text_preview(text, limit=preview_limit)


def _debug_hash_text(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:16]


def _debug_vfcurve_blob_summary(hex_blob):
    text = str(hex_blob).strip()
    if not text:
        return "vfcurve=<empty>"

    parts = [
        f"chars={len(text)}",
        f"sha256={hash_afterburner_vfcurve_hex(text)[:16]}",
    ]
    if len(text) % 2 != 0:
        parts.append("hex-odd-length")
    try:
        header, points, tail = parse_vfcurve_blob(text)
    except Exception as exc:
        parts.append(f"parse-error={exc}")
    else:
        parts.append(
            "header="
            f"{int(header['magic_u32'])}/{int(header['point_count_hint'])}/{int(header['flags_u32'])}"
        )
        parts.append(f"points={len(points)}")
        parts.append(f"tail-nonzero={len(tail)}")
    parts.append(f"preview={_debug_render_text_preview(text, limit=72)}")
    return "vfcurve-blob " + " ".join(parts)


def _debug_fan_curve_blob_summary(hex_blob):
    text = str(hex_blob).strip()
    if not text:
        return "fan-curve=<empty>"

    parts = [
        f"chars={len(text)}",
        f"sha256={_debug_hash_text(text)}",
    ]
    if len(text) % 2 != 0:
        parts.append("hex-odd-length")
    try:
        parsed = parse_sw_auto_fan_curve(text)
    except Exception as exc:
        parts.append(f"parse-error={exc}")
    else:
        parts.append(
            "header="
            f"{int(parsed['magic_u32'])}/{int(parsed['point_count'])}/{int(parsed['flags_u32'])}"
        )
        parts.append(f"points={len(parsed['points'])}")
    parts.append(f"preview={_debug_render_text_preview(text, limit=72)}")
    return "fan-curve-blob " + " ".join(parts)


def _debug_selection_reason_summary(
    section_info,
    *,
    requested_section="",
):
    reasons = []
    normalized_requested = _normalized_afterburner_section_name(requested_section)
    normalized_section = _normalized_afterburner_section_name(
        section_info.get("section")
    )
    if normalized_requested and normalized_requested != normalized_section:
        reasons.append("requested-section-mismatch")
    if not section_info.get("is_manual_candidate"):
        reasons.append("not-manual")
    if section_info.get("flatten_target") is None:
        reasons.append("no-flat-tail")
    validation = section_info.get("flatten_validation")
    if validation is None:
        reasons.append("no-undervolt-validation")
    elif not validation.get("valid"):
        reasons.append(
            "invalid-undervolt="
            + _debug_render_text_preview(validation.get("reason", "invalid"), limit=88)
        )

    default_eligible = bool(
        section_info.get("is_manual_candidate")
        and section_info.get("flatten_target") is not None
        and validation
        and validation.get("valid")
    )
    dangerous_eligible = bool(section_info.get("is_manual_candidate"))
    return (
        f"default-eligible={'yes' if default_eligible else 'no'} "
        f"dangerous-eligible={'yes' if dangerous_eligible else 'no'} "
        f"reasons={';'.join(reasons) if reasons else 'selected-candidate'}"
    )


def _debug_log_device_profile_section_dump(profile_path, section_info):
    try:
        resolved_section, raw_values = load_afterburner_profile_section(
            profile_path=profile_path,
            section=section_info["section"],
        )
    except Exception as exc:
        debug_exception(
            f"failed to dump raw section values for {Path(profile_path).name}:{section_info['section']}",
            exc,
        )
        return

    debug_log(
        f"{Path(profile_path).name}:{resolved_section}: raw-key-count={len(raw_values)}"
    )
    for key, value in raw_values.items():
        normalized_key = str(key).strip().lower()
        if normalized_key == "vfcurve":
            debug_log(
                f"{Path(profile_path).name}:{resolved_section}: "
                + _debug_vfcurve_blob_summary(value)
            )
            debug_log(
                f"{Path(profile_path).name}:{resolved_section}: "
                f"VFCurve-raw={_debug_render_text_or_full(value)}"
            )
            continue
        debug_log(
            f"{Path(profile_path).name}:{resolved_section}: "
            f"{key}={_debug_render_text_or_full(value)}"
        )


def _debug_log_fan_profile_dump(profile_path):
    try:
        lines = Path(profile_path).read_text(errors="ignore").splitlines()
    except Exception as exc:
        debug_exception(f"failed to read the fan profile {profile_path}", exc)
        return

    fan_lines = [
        line.strip()
        for line in lines
        if "=" in line and line.strip().startswith("SwAutoFanControl")
    ]
    debug_log(f"{Path(profile_path).name}: raw-fan-key-count={len(fan_lines)}")
    for line in fan_lines:
        key, value = line.split("=", 1)
        normalized_key = key.strip().lower()
        if normalized_key in ("swautofancontrolcurve", "swautofancontrolcurve2"):
            debug_log(
                f"{Path(profile_path).name}: " + _debug_fan_curve_blob_summary(value)
            )
            debug_log(
                f"{Path(profile_path).name}: "
                f"{key.strip()}-raw={_debug_render_text_or_full(value)}"
            )
            continue
        debug_log(
            f"{Path(profile_path).name}: "
            f"{key.strip()}={_debug_render_text_or_full(value)}"
        )


def _debug_log_runtime_environment():
    if not DEBUG_LOG_ENABLED:
        return
    debug_log(f"python={sys.version.split()[0]} executable={sys.executable}")
    debug_log(f"platform={platform.platform()}")
    debug_log(
        f"user={pwd.getpwuid(os.getuid()).pw_name} uid={os.getuid()} "
        f"euid={os.geteuid()} sudo-user={os.environ.get('SUDO_USER', '').strip() or '(none)'}"
    )
    debug_log(
        f"path-has-nvidia-smi={'yes' if shutil.which('nvidia-smi') else 'no'} "
        f"path-has-systemctl={'yes' if shutil.which('systemctl') else 'no'}"
    )

    try:
        version_result = subprocess.run(
            [NVIDIA_SMI, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=stable_subprocess_env(),
            check=False,
        )
    except Exception as exc:
        debug_exception("failed to run nvidia-smi --version", exc)
    else:
        version_text = (
            (version_result.stdout or version_result.stderr)
            .strip()
            .replace("\n", " | ")
        )
        debug_log(
            f"nvidia-smi-version rc={version_result.returncode} "
            f"text={_debug_render_text_preview(version_text, limit=140)}"
        )

    try:
        query_result = subprocess.run(
            [
                NVIDIA_SMI,
                "--query-gpu=index,name,driver_version,pci.bus_id",
                "--format=csv,noheader",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=stable_subprocess_env(),
            check=False,
        )
    except Exception as exc:
        debug_exception("failed to query nvidia-smi gpu metadata", exc)
    else:
        lines = [
            line.strip() for line in query_result.stdout.splitlines() if line.strip()
        ]
        debug_log(
            f"nvidia-smi-gpu-query rc={query_result.returncode} count={len(lines)}"
        )
        for line in lines:
            debug_log(f"nvidia-smi-gpu={line}")


def debug_effective_runtime_options(
    *, config_path, gpu_index, afterburner_runtime_options
):
    if not DEBUG_LOG_ENABLED:
        return
    debug_log(
        "effective-runtime="
        f"config-path={Path(config_path).expanduser().resolve()} "
        f"gpu-index={gpu_index} "
        f"afterburner-root={afterburner_runtime_options.get('afterburner_root') or '(none)'} "
        f"section={afterburner_runtime_options.get('afterburner_profile') or '(auto)'} "
        f"device-profile={afterburner_runtime_options.get('afterburner_device_profile') or '(auto)'} "
        f"power-limit-override={afterburner_runtime_options.get('power_limit_override_w')} "
        f"preserve-vf-below-mv={afterburner_runtime_options.get('preserve_base_below_mv')} "
        f"auto-uv-max-drop-pct={afterburner_runtime_options.get('auto_uv_max_drop_pct')} "
        f"dangerously-skip-validation={bool(afterburner_runtime_options.get('dangerously_skip_validation'))}"
    )


def _debug_describe_file(path):
    try:
        exists = path.exists()
    except Exception as exc:
        return f"path={path} exists=error({exc})"

    details = [f"path={path}", f"exists={'yes' if exists else 'no'}"]
    if exists:
        try:
            details.append(f"is-file={'yes' if path.is_file() else 'no'}")
            details.append(f"is-dir={'yes' if path.is_dir() else 'no'}")
            details.append(f"size={path.stat().st_size}")
        except Exception as exc:
            details.append(f"stat-error={exc}")
    return " ".join(details)


def _debug_describe_afterburner_section(section_info):
    flags = []
    if section_info.get("is_builtin"):
        flags.append("builtin")
    if section_info.get("matches_defaults"):
        flags.append("matches-defaults")
    if section_info.get("matches_startup"):
        flags.append("matches-startup")
    if section_info.get("is_manual_candidate"):
        flags.append("manual")
    if section_info.get("is_valid_manual_candidate"):
        flags.append("valid")
    flag_text = ",".join(flags) if flags else "none"
    analysis = section_info.get("analysis")
    if isinstance(analysis, dict) and "point_count" in analysis:
        analysis_text = describe_afterburner_vfcurve_analysis(analysis)
    else:
        analysis_text = "unknown"
    return (
        f"section={section_info.get('section')} "
        f"flags={flag_text} "
        f"flatten={describe_afterburner_dynamic_lock(section_info.get('flatten_target'))} "
        f"validation={describe_afterburner_flatten_validation(section_info.get('flatten_validation'))} "
        f"analysis={analysis_text}"
    )


def emit_afterburner_debug_snapshot(
    *,
    afterburner_root,
    requested_section,
    device_profile_hint,
    dangerously_skip_validation=False,
):
    if not DEBUG_LOG_ENABLED:
        return

    debug_log("Afterburner debug snapshot begin")
    debug_log(f"requested-root={afterburner_root}")
    debug_log(f"requested-section={requested_section or '(auto)'}")
    debug_log(f"device-profile-hint={device_profile_hint or '(auto)'}")
    debug_log(
        "selection-mode="
        + (
            "dangerously-skip-validation"
            if dangerously_skip_validation
            else "default-validation"
        )
    )

    try:
        resolved_root = resolve_afterburner_root(afterburner_root)
    except Exception as exc:
        debug_exception("failed to resolve the Afterburner root", exc)
        return

    debug_log(f"resolved-root={resolved_root}")
    try:
        problems = validate_afterburner_export_root(resolved_root)
    except Exception as exc:
        debug_exception("failed to validate the Afterburner root", exc)
        problems = []
    if problems:
        debug_log("root-validation-problems=" + "; ".join(problems))
    else:
        debug_log("root-validation=ok")

    debug_log(_debug_describe_file(afterburner_global_profile(resolved_root)))
    debug_log(_debug_describe_file(afterburner_profiles_dir(resolved_root)))

    try:
        fan_profile_path = resolve_afterburner_fan_profile(
            afterburner_root=resolved_root
        )
        debug_log(f"selected-fan-profile={fan_profile_path}")
        fan_settings = load_afterburner_fan_settings(fan_profile_path)
        debug_log(
            "fan-settings="
            f"sw-auto={fan_settings['sw_auto_enabled']} "
            f"period-ms={fan_settings['period_ms']} "
            f"flags=0x{int(fan_settings['flags_u32']):08x} "
            f"curve-points={len(fan_settings['curve']['points'])} "
            f"reference-points={len(fan_settings['curve2']['points'])}"
        )
        _debug_log_fan_profile_dump(fan_profile_path)
    except Exception as exc:
        debug_exception("failed to parse the Afterburner fan profile", exc)

    try:
        device_profiles = discover_afterburner_device_profiles(resolved_root)
    except Exception as exc:
        debug_exception("failed to enumerate Afterburner device profiles", exc)
        return

    if not device_profiles:
        debug_log("device-profiles=none")
        return

    debug_log(f"device-profiles-found={len(device_profiles)}")
    for candidate in device_profiles:
        debug_log(_debug_describe_file(candidate))
        try:
            sections = discover_afterburner_vf_sections(candidate)
        except Exception as exc:
            debug_exception(f"failed to inspect device profile {candidate.name}", exc)
            continue

        debug_log(f"{candidate.name}: vf-sections={len(sections)}")
        for section_info in sections:
            debug_log(
                f"{candidate.name}: {_debug_describe_afterburner_section(section_info)} "
                f"{_debug_selection_reason_summary(section_info, requested_section=requested_section)}"
            )
            try:
                settings = load_afterburner_profile_settings(
                    profile_path=candidate,
                    section=section_info["section"],
                )
            except Exception as exc:
                debug_exception(
                    f"failed to parse settings for {candidate.name}:{section_info['section']}",
                    exc,
                )
                continue
            debug_log(
                f"{candidate.name}:{section_info['section']}: "
                f"{describe_afterburner_profile_settings(settings)}"
            )
            _debug_log_device_profile_section_dump(candidate, section_info)

    debug_log("Afterburner debug snapshot end")
