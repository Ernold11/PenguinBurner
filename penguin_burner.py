#!/usr/bin/env python3

import argparse
import atexit
import hashlib
import ctypes
import os
import platform
from pathlib import Path
import pwd
import signal
import shlex
import shutil
import subprocess
import sys
import time
import traceback
import tomllib

from afterburner_fan_curve import (
    format_curve_points as format_afterburner_curve_points,
    load_afterburner_fan_settings,
    parse_sw_auto_fan_curve,
    resolve_afterburner_fan_profile,
)
from afterburner_vfcurve import (
    discover_afterburner_vf_sections,
    derive_afterburner_dynamic_lock,
    describe_afterburner_dynamic_lock,
    describe_afterburner_flatten_validation,
    describe_afterburner_profile_settings,
    describe_afterburner_vfcurve_analysis,
    hash_afterburner_vfcurve_hex,
    load_afterburner_profile_settings,
    load_afterburner_profile_section,
    parse_vfcurve_blob,
    resolve_afterburner_vf_source,
)
from hidden_nvapi_vf import create_hidden_vf_curve_reader
from hidden_nvml_voltage import create_hidden_voltage_reader
from import_afterburner_fan_curve import build_imported_fan_section
from import_afterburner_vf_curve import (
    apply_plan,
    apply_afterburner_curve_to_reader,
    build_plan,
    ensure_afterburner_root_configured,
    load_afterburner_runtime_options,
)
from nvml_gpu_policy import (
    NvmlGpuPolicyController,
    apply_translated_gpu_policy,
    describe_translated_gpu_policy,
    translate_afterburner_gpu_policy,
)
from penguin_burner_paths import (
    afterburner_global_profile,
    afterburner_profiles_dir,
    default_runtime_config_path,
    discover_afterburner_device_profiles,
    resolve_afterburner_root,
    validate_afterburner_export_root,
)


NVML_SUCCESS = 0
NVML_TEMPERATURE_GPU = 0
NVML_CLOCK_GRAPHICS = 0
NVML_CLOCK_MEM = 2
NVIDIA_SMI = shutil.which("nvidia-smi") or "nvidia-smi"
SYSTEMD_RUN = shutil.which("systemd-run") or "systemd-run"
SYSTEMCTL = shutil.which("systemctl") or "systemctl"
BASH = shutil.which("bash") or "/usr/bin/bash"
PENGUIN_BURNER_UNIT_NAME = "PenguinBurner"
PENGUIN_BURNER_FOREGROUND_ENV = "PENGUIN_BURNER_FOREGROUND"
DEFAULT_JOURNAL_HOURS = 4
DEBUG_LOG_ENABLED = False
DEBUG_LOG_PATH = None
DEBUG_LOG_FILE = None
DEBUG_LOG_MAX_BYTES = 700 * 1024
DEBUG_LOG_BYTES_WRITTEN = 0
DEBUG_LOG_TRUNCATED = False


class NvmlError(RuntimeError):
    pass


def log(message):
    text = str(message)
    print(text, flush=True)
    _write_debug_log_line(text)


def debug_log(message):
    if not DEBUG_LOG_ENABLED:
        return
    text = f"[debug] {message}"
    _write_debug_log_line(text)


def debug_log_enabled():
    return bool(DEBUG_LOG_ENABLED)


def current_debug_log_path():
    return DEBUG_LOG_PATH


def close_debug_log():
    global DEBUG_LOG_FILE
    if DEBUG_LOG_FILE is None:
        return
    DEBUG_LOG_FILE.close()
    DEBUG_LOG_FILE = None


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


def enable_debug_logging(config_path, *, argv=None):
    global DEBUG_LOG_ENABLED, DEBUG_LOG_PATH, DEBUG_LOG_FILE, DEBUG_LOG_BYTES_WRITTEN, DEBUG_LOG_TRUNCATED
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
    if not DEBUG_LOG_ENABLED:
        return
    debug_log(f"{context}: {exc.__class__.__name__}: {exc}")
    for line in traceback.format_exception(type(exc), exc, exc.__traceback__):
        for fragment in line.rstrip().splitlines():
            debug_log(f"traceback: {fragment}")


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
    normalized_section = _normalized_afterburner_section_name(section_info.get("section"))
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

    debug_log(f"{Path(profile_path).name}:{resolved_section}: raw-key-count={len(raw_values)}")
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
                f"{Path(profile_path).name}: "
                + _debug_fan_curve_blob_summary(value)
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
            check=False,
        )
    except Exception as exc:
        debug_exception("failed to run nvidia-smi --version", exc)
    else:
        version_text = (version_result.stdout or version_result.stderr).strip().replace("\n", " | ")
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
            check=False,
        )
    except Exception as exc:
        debug_exception("failed to query nvidia-smi gpu metadata", exc)
    else:
        lines = [line.strip() for line in query_result.stdout.splitlines() if line.strip()]
        debug_log(f"nvidia-smi-gpu-query rc={query_result.returncode} count={len(lines)}")
        for line in lines:
            debug_log(f"nvidia-smi-gpu={line}")


def _debug_log_effective_runtime_options(*, config_path, gpu_index, afterburner_runtime_options):
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
        f"preserve-vf-below-mv={afterburner_runtime_options.get('preserve_vanilla_below_mv')} "
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
        + ("dangerously-skip-validation" if dangerously_skip_validation else "default-validation")
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
        fan_profile_path = resolve_afterburner_fan_profile(afterburner_root=resolved_root)
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


atexit.register(close_debug_log)


def prompt_yes_no(prompt, *, default):
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        entered = input(f"{prompt} {suffix}: ").strip().lower()
        if DEBUG_LOG_ENABLED:
            debug_log(f"prompt={prompt} answer={entered or '<enter>'}")
        if not entered:
            return bool(default)
        if entered in ("y", "yes"):
            return True
        if entered in ("n", "no"):
            return False
        print("Please answer y or n.", flush=True)


def parse_runtime_flags(argv):
    foreground = False
    daemonize = False
    install_systemd_service = False
    uninstall_systemd_service = False
    journal_hours = DEFAULT_JOURNAL_HOURS
    passthrough = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--foreground":
            foreground = True
            index += 1
            continue
        if arg == "--daemonize":
            daemonize = True
            index += 1
            continue
        if arg == "--install-systemd-service":
            install_systemd_service = True
            index += 1
            continue
        if arg == "--uninstall-systemd-service":
            uninstall_systemd_service = True
            index += 1
            continue
        if arg == "--journal-hours":
            if index + 1 >= len(argv):
                raise NvmlError("--journal-hours requires a value")
            index += 1
            arg = f"--journal-hours={argv[index]}"
        if arg.startswith("--journal-hours="):
            value = arg.split("=", 1)[1].strip()
            if not value:
                raise NvmlError("--journal-hours requires a value")
            journal_hours = max(1, int(float(value)))
            index += 1
            continue
        passthrough.append(arg)
        index += 1
    if install_systemd_service and uninstall_systemd_service:
        raise NvmlError("choose either --install-systemd-service or --uninstall-systemd-service")
    return {
        "foreground": foreground,
        "daemonize": daemonize,
        "install_systemd_service": install_systemd_service,
        "uninstall_systemd_service": uninstall_systemd_service,
        "journal_hours": journal_hours,
        "passthrough": passthrough,
    }


def running_under_systemd_service():
    return os.environ.get(PENGUIN_BURNER_FOREGROUND_ENV) == "1"


def systemd_is_available():
    return Path("/run/systemd/system").exists() and shutil.which("systemd-run") is not None


def journalctl_follow_command(hours):
    return f"journalctl -u {PENGUIN_BURNER_UNIT_NAME}.service --since \"-{int(hours)} hours\" -f"


def systemd_service_unit_path():
    return Path("/etc/systemd/system") / f"{PENGUIN_BURNER_UNIT_NAME}.service"


def _invoking_user_name():
    sudo_user = os.environ.get("SUDO_USER", "").strip()
    if sudo_user:
        return sudo_user
    return pwd.getpwuid(os.getuid()).pw_name


def launcher_script_path():
    path = Path(__file__).resolve().with_name("penguin_burner.sh")
    if not path.is_file():
        raise NvmlError(f"launcher script not found: {path}")
    return path


def _format_systemd_exec(args):
    rendered = []
    for arg in args:
        text = str(arg).replace("%", "%%")
        rendered.append(shlex.quote(text))
    return " ".join(rendered)


def run_checked_subprocess(args):
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        output = (result.stdout.strip() or result.stderr.strip()).strip()
        command_text = " ".join(shlex.quote(str(arg)) for arg in args)
        raise NvmlError(f"{command_text} failed: {output or result.returncode}")
    return result


def build_systemd_service_unit(argv):
    script_path = launcher_script_path()
    sudo_user = _invoking_user_name()
    exec_start = _format_systemd_exec([BASH, str(script_path), "--foreground", *argv])
    return (
        "[Unit]\n"
        "Description=PenguinBurner runtime daemon\n"
        "After=multi-user.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"Environment=PENGUIN_BURNER_FOREGROUND=1\n"
        f"Environment=SUDO_USER={sudo_user}\n"
        "WorkingDirectory=/\n"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n"
        "RestartSec=2\n"
        "StandardOutput=journal\n"
        "StandardError=journal\n"
        f"SyslogIdentifier={PENGUIN_BURNER_UNIT_NAME}\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def install_systemd_service(argv, *, journal_hours):
    if not systemd_is_available():
        raise NvmlError(
            "systemd service install is unavailable on this system."
        )
    if os.geteuid() != 0:
        raise NvmlError(
            "systemd service install requires root privileges. Re-run with sudo."
        )

    unit_path = systemd_service_unit_path()
    unit_path.write_text(build_systemd_service_unit(argv))
    run_checked_subprocess([SYSTEMCTL, "daemon-reload"])
    subprocess.run(
        [SYSTEMCTL, "reset-failed", unit_path.name],
        capture_output=True,
        text=True,
        check=False,
    )
    run_checked_subprocess([SYSTEMCTL, "enable", "--now", unit_path.name])
    log(f"Installed and enabled {unit_path.name} at {unit_path}.")
    log(f"Follow the journal with: {journalctl_follow_command(journal_hours)}")


def uninstall_systemd_service():
    if not systemd_is_available():
        raise NvmlError(
            "systemd service uninstall is unavailable on this system."
        )
    if os.geteuid() != 0:
        raise NvmlError(
            "systemd service uninstall requires root privileges. Re-run with sudo."
        )

    unit_path = systemd_service_unit_path()
    subprocess.run([SYSTEMCTL, "disable", "--now", unit_path.name], check=False)
    if unit_path.exists():
        unit_path.unlink()
    run_checked_subprocess([SYSTEMCTL, "daemon-reload"])
    subprocess.run(
        [SYSTEMCTL, "reset-failed", unit_path.name],
        capture_output=True,
        text=True,
        check=False,
    )
    log(f"Removed {unit_path.name}.")


def daemonize_with_systemd(argv, *, journal_hours):
    if not systemd_is_available():
        raise NvmlError(
            "systemd background mode is unavailable on this system. "
            "Run PenguinBurner with --foreground or use a systemd-based system."
        )
    if os.geteuid() != 0:
        raise NvmlError(
            "automatic systemd daemon mode requires root privileges. "
            "Re-run PenguinBurner with sudo."
        )

    script_path = launcher_script_path()
    sudo_user = os.environ.get("SUDO_USER", "").strip()

    subprocess.run(
        [SYSTEMCTL, "stop", f"{PENGUIN_BURNER_UNIT_NAME}.service"],
        capture_output=True,
        text=True,
        check=False,
    )

    result = subprocess.run(
        [
            SYSTEMD_RUN,
            "--unit",
            PENGUIN_BURNER_UNIT_NAME,
            "--collect",
            "--service-type=simple",
            "--description",
            "PenguinBurner runtime daemon",
            "--property=WorkingDirectory=/",
            "--property=Restart=on-failure",
            "--property=RestartSec=2",
            "--property=StandardOutput=journal",
            "--property=StandardError=journal",
            "--property=SyslogIdentifier=PenguinBurner",
            "--setenv",
            f"{PENGUIN_BURNER_FOREGROUND_ENV}=1",
            "--setenv",
            f"SUDO_USER={sudo_user}",
            BASH,
            str(script_path),
            "--foreground",
            *argv,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    output = (result.stdout.strip() or result.stderr.strip()).strip()
    if result.returncode != 0:
        raise NvmlError(
            "failed to daemonize PenguinBurner with systemd: "
            + (output or str(result.returncode))
        )

    unit_name = f"{PENGUIN_BURNER_UNIT_NAME}.service"
    if output:
        log(output)
    log(f"PenguinBurner daemonized under systemd as {unit_name}.")
    log(f"Follow the journal with: {journalctl_follow_command(journal_hours)}")


def get_config_path():
    return default_runtime_config_path()


def default_config():
    return {
        "gpu": {
            "index": 0,
            "enable_persistence_mode": True,
        },
        "fan": {
            "poll_interval_s": 2,
            "hysteresis_c": 2.0,
            "mode": "linear",
            "min_fan_speed_pct": 20,
            "max_fan_speed_pct": 100,
            "max_step_up_pct_per_s": 25.0,
            "max_step_down_pct_per_s": 15.0,
            "manual_enable_temp_c": 55.0,
            "auto_restore_temp_c": 50.0,
            "emergency_auto_override_temp_c": 80.0,
            "emergency_auto_resume_temp_c": 75.0,
            "force_update_every_poll": False,
            "curve": [
                [55, 30],
                [65, 35],
                [70, 40],
                [80, 45],
            ],
        },
    }


def load_config(config_path=None):
    if config_path is None:
        config_path = get_config_path()
    else:
        config_path = Path(config_path).expanduser()
    config = default_config()

    if not config_path.exists():
        return config, config_path

    with config_path.open("rb") as config_file:
        loaded = tomllib.load(config_file)

    for section in ("gpu", "fan"):
        values = loaded.get(section)
        if isinstance(values, dict):
            config[section].update(values)

    return config, config_path


def parse_main_args(argv):
    parser = argparse.ArgumentParser(
        prog=Path(__file__).name,
        description="PenguinBurner runtime and Afterburner inspection utility.",
        epilog=(
            "System-level flags handled before runtime parsing:\n"
            "  --foreground\n"
            "  --daemonize\n"
            "  --install-systemd-service\n"
            "  --uninstall-systemd-service\n"
            f"  --journal-hours N (default {DEFAULT_JOURNAL_HOURS})"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        default=str(get_config_path()),
        help="Runtime config path to read defaults from",
    )
    parser.add_argument(
        "--gpu-index",
        type=int,
        default=None,
        help="Override the configured GPU index",
    )
    parser.add_argument(
        "--afterburner-dir",
        default="",
        help="Path to the MSI Afterburner root directory",
    )
    parser.add_argument(
        "--profile-section",
        "--section",
        dest="profile_section",
        default="",
        help="Optional saved Afterburner profile section such as profile2",
    )
    parser.add_argument(
        "--afterburner-device-profile",
        default="",
        help="Optional device profile file under Profiles/ to inspect or use",
    )
    parser.add_argument(
        "--power-limit-override-w",
        type=int,
        default=None,
        help="Optional manual power-limit cap in watts for translation preview",
    )
    parser.add_argument(
        "--preserve-vf-below-mv",
        "--preserve-vanilla-vf-below-mv",
        dest="preserve_vanilla_below_mv",
        type=int,
        default=None,
        help=(
            "Keep the stock/base Linux VF curve at and below this inclusive "
            "voltage; useful if repeated Afterburner curve edits disturbed "
            "idle or low-voltage scaling"
        ),
    )
    parser.add_argument(
        "--dangerously-skip-validation",
        action="store_true",
        help=(
            "Bypass the default flat-tail and undervolt checks when selecting "
            "the saved Afterburner profile; advanced and not recommended"
        ),
    )
    parser.add_argument(
        "--debug-log",
        action="store_true",
        help=(
            "Write a verbose dry-run and first-import diagnostic log next to "
            "the selected config file under debug-logs/; with the default "
            "config this is ~/.config/PenguinBurner/debug-logs"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Inspect Afterburner fan/VF data and draw dry-run previews without "
            "touching GPU state; recommended first step and does not require sudo"
        ),
    )
    return parser.parse_args(argv)


def check(rc, name):
    if rc != NVML_SUCCESS:
        raise NvmlError(f"{name} failed with NVML error {rc}")


def clamp(value, lower, upper):
    return max(lower, min(value, upper))


def validate_curve(curve):
    if len(curve) < 2:
        raise NvmlError("curve must contain at least two points")

    last_temp = None
    last_speed = None
    for temp_c, speed_pct in curve:
        if last_temp is not None and temp_c <= last_temp:
            raise NvmlError("curve temperatures must be strictly increasing")
        if not 0 <= speed_pct <= 100:
            raise NvmlError("curve fan speeds must be in the range 0..100")
        if last_speed is not None and speed_pct < last_speed:
            raise NvmlError("curve fan speeds must not decrease as temperature rises")
        last_temp = temp_c
        last_speed = speed_pct


def format_curve_points(curve):
    return ", ".join(f"{temp_c:.0f}C->{speed_pct:.0f}%" for temp_c, speed_pct in curve)


def format_curve_temp(temp_c):
    if abs(temp_c - round(temp_c)) < 0.01:
        return f"{int(round(temp_c))}C"
    return f"{temp_c:.2f}C"


def select_expected_vf_samples(plan, *, max_samples=8):
    candidates = [
        item
        for item in plan
        if int(item["new_offset_mhz"]) != 0
    ]
    candidates.sort(key=lambda item: abs(int(item["new_offset_mhz"])), reverse=True)
    return candidates[:max_samples]


def detect_vf_curve_reset(vf_curve_reader, expected_samples, *, tolerance_mhz=1):
    if vf_curve_reader is None or not expected_samples:
        return []

    current_points = {
        int(point["index"]): int(point["current_offset_khz"] // 1000)
        for point in vf_curve_reader.editable_core_points()
    }
    mismatches = []
    for sample in expected_samples:
        index = int(sample["index"])
        expected_offset_mhz = int(sample["new_offset_mhz"])
        current_offset_mhz = int(current_points.get(index, 0))
        if abs(current_offset_mhz - expected_offset_mhz) > int(tolerance_mhz):
            mismatches.append(
                {
                    "index": index,
                    "expected_offset_mhz": expected_offset_mhz,
                    "current_offset_mhz": current_offset_mhz,
                    "voltage_mv": int(sample["voltage_mv"]),
                }
            )
    return mismatches


def build_effective_manual_curve(
    curve,
    manual_enable_temp_c,
    effective_min_fan_speed_pct,
    effective_max_fan_speed_pct,
    mode,
):
    start_speed_pct = clamp(
        speed_for_temp(manual_enable_temp_c, curve, mode=mode),
        effective_min_fan_speed_pct,
        effective_max_fan_speed_pct,
    )
    effective_curve = [(float(manual_enable_temp_c), float(start_speed_pct))]

    for temp_c, speed_pct in curve:
        if temp_c <= manual_enable_temp_c:
            continue
        clamped_speed_pct = clamp(
            float(speed_pct),
            effective_min_fan_speed_pct,
            effective_max_fan_speed_pct,
        )
        last_temp_c, last_speed_pct = effective_curve[-1]
        if abs(clamped_speed_pct - last_speed_pct) < 0.001:
            continue
        effective_curve.append((float(temp_c), float(clamped_speed_pct)))

    return effective_curve


def describe_fan_curve_state(
    current_temp_c,
    effective_curve,
    manual_mode_active,
    emergency_auto_mode_active,
    emergency_auto_resume_temp_c,
):
    if emergency_auto_mode_active:
        return (
            "fan_curve_state=emergency-auto "
            f"next_fan_step={format_curve_temp(emergency_auto_resume_temp_c)}->resume-custom"
        )

    if not manual_mode_active:
        takeover_temp_c, takeover_speed_pct = effective_curve[0]
        return (
            "fan_curve_state=hardware-auto "
            f"next_fan_step={format_curve_temp(takeover_temp_c)}->{takeover_speed_pct:.0f}%"
        )

    if len(effective_curve) == 1:
        temp_c, speed_pct = effective_curve[0]
        return (
            f"fan_curve_state={format_curve_temp(temp_c)}+:{speed_pct:.0f}% "
            "next_fan_step=none"
        )

    for index in range(len(effective_curve) - 1):
        left_temp_c, left_speed_pct = effective_curve[index]
        right_temp_c, right_speed_pct = effective_curve[index + 1]
        if current_temp_c < right_temp_c:
            return (
                f"fan_curve_state={format_curve_temp(left_temp_c)}-{format_curve_temp(right_temp_c)}:"
                f"{left_speed_pct:.0f}-{right_speed_pct:.0f}% "
                f"next_fan_step={format_curve_temp(right_temp_c)}->{right_speed_pct:.0f}%"
            )

    last_temp_c, last_speed_pct = effective_curve[-1]
    return (
        f"fan_curve_state={format_curve_temp(last_temp_c)}+:{last_speed_pct:.0f}% "
        "next_fan_step=none"
    )


def speed_for_temp(temp_c, curve, mode):
    if temp_c <= curve[0][0]:
        return curve[0][1]

    for index in range(1, len(curve)):
        left_temp, left_speed = curve[index - 1]
        right_temp, right_speed = curve[index]

        if temp_c <= right_temp:
            if mode == "step":
                return left_speed

            span = right_temp - left_temp
            t = (temp_c - left_temp) / span
            return left_speed + (right_speed - left_speed) * t

    return curve[-1][1]


def apply_hysteresis(current_temp_c, raw_target_speed, last_temp_c, last_speed, hysteresis_c):
    if last_temp_c is None or last_speed is None or hysteresis_c <= 0.0:
        return raw_target_speed

    if raw_target_speed >= last_speed:
        return raw_target_speed

    if current_temp_c > last_temp_c:
        return raw_target_speed

    if (last_temp_c - current_temp_c) < hysteresis_c:
        return float(last_speed)

    return raw_target_speed


def limit_speed_change(target_speed, last_speed, elapsed_s, max_step_up_pct_per_s, max_step_down_pct_per_s):
    if last_speed is None or elapsed_s <= 0.0:
        return target_speed

    limited_speed = float(target_speed)

    if limited_speed > last_speed and max_step_up_pct_per_s > 0.0:
        max_up = max_step_up_pct_per_s * elapsed_s
        limited_speed = min(limited_speed, last_speed + max_up)

    if limited_speed < last_speed and max_step_down_pct_per_s > 0.0:
        max_down = max_step_down_pct_per_s * elapsed_s
        limited_speed = max(limited_speed, last_speed - max_down)

    return limited_speed


def get_reported_fan_speeds(nvml, device, fan_count):
    fan_speeds = []

    for fan_idx in range(fan_count):
        speed = ctypes.c_uint()
        rc = nvml.nvmlDeviceGetFanSpeed_v2(device, ctypes.c_uint(fan_idx), ctypes.byref(speed))
        if rc != NVML_SUCCESS:
            fan_speeds = []
            break
        fan_speeds.append(int(speed.value))

    if fan_speeds:
        return fan_speeds

    if fan_count == 1:
        speed = ctypes.c_uint()
        rc = nvml.nvmlDeviceGetFanSpeed(device, ctypes.byref(speed))
        if rc == NVML_SUCCESS:
            return [int(speed.value)]

    return None


def get_power_draw_w(nvml, device):
    power_mw = ctypes.c_uint()
    rc = nvml.nvmlDeviceGetPowerUsage(device, ctypes.byref(power_mw))
    if rc != NVML_SUCCESS:
        return None
    return power_mw.value / 1000.0


def get_graphics_clock_mhz(nvml, device):
    clock_mhz = ctypes.c_uint()
    rc = nvml.nvmlDeviceGetClockInfo(device, ctypes.c_uint(NVML_CLOCK_GRAPHICS), ctypes.byref(clock_mhz))
    if rc != NVML_SUCCESS:
        return None
    return int(clock_mhz.value)


def get_memory_clock_mhz(nvml, device):
    clock_mhz = ctypes.c_uint()
    rc = nvml.nvmlDeviceGetClockInfo(device, ctypes.c_uint(NVML_CLOCK_MEM), ctypes.byref(clock_mhz))
    if rc != NVML_SUCCESS:
        return None
    return int(clock_mhz.value)


def format_vf_curve_comparison(vf_curve_reader, graphics_clock_mhz, voltage_uv):
    point = vf_curve_reader.find_nearest_point(graphics_clock_mhz, voltage_uv)
    if point is None:
        return ""

    point_freq_mhz = int(point["freq_khz"] // 1000)
    point_voltage_mv = int(point["voltage_uv"] // 1000)
    point_base_freq_mhz = int(point["base_freq_khz"] // 1000)
    point_offset_mhz = int(point["current_offset_khz"] // 1000)

    vanilla_point = min(
        vf_curve_reader.editable_core_points(),
        key=lambda candidate: abs(int(candidate["base_freq_khz"]) - int(graphics_clock_mhz) * 1000),
    )
    vanilla_clock_mhz = int(vanilla_point["base_freq_khz"] // 1000)
    vanilla_voltage_mv = int(vanilla_point["voltage_uv"] // 1000)
    uv_delta_mv = int(point_voltage_mv - vanilla_voltage_mv)

    return (
        f"vf_point={point_freq_mhz}MHz@{point_voltage_mv}mV "
        f"vf_offset={point_offset_mhz:+d}MHz "
        f"vf_vanilla={vanilla_clock_mhz}MHz@{vanilla_voltage_mv}mV "
        f"uv={uv_delta_mv:+d}mV "
    )


def format_clock_offsets(gpu_policy_controller):
    if gpu_policy_controller is None:
        return ""

    try:
        offsets = gpu_policy_controller.get_clock_offsets()
    except Exception:
        return ""

    mem_clk_vf_offset_mhz = offsets.get("mem_clk_vf_offset_mhz")
    if mem_clk_vf_offset_mhz is None:
        return ""
    return f"mem_vf_offset={int(mem_clk_vf_offset_mhz):+d}MHz "


def format_clock_ceiling_state(clock_ceiling_controller):
    if clock_ceiling_controller is None:
        return ""
    return clock_ceiling_controller.telemetry_text()


def format_telemetry(
    nvml,
    device,
    fan_count,
    current_temp_c,
    voltage_reader=None,
    vf_curve_reader=None,
    gpu_policy_controller=None,
    power_draw_w=None,
    clock_ceiling_controller=None,
):
    reported_fan_speeds = get_reported_fan_speeds(nvml, device, fan_count)
    if reported_fan_speeds is None:
        fan_text = "n/a"
    else:
        fan_text = "/".join(f"{speed}%" for speed in reported_fan_speeds)

    if power_draw_w is None:
        power_draw_w = get_power_draw_w(nvml, device)
    power_text = "n/a" if power_draw_w is None else f"{power_draw_w:.2f}W"

    graphics_clock_mhz = get_graphics_clock_mhz(nvml, device)
    if graphics_clock_mhz is None:
        clock_text = "n/a"
    else:
        clock_text = f"{graphics_clock_mhz}MHz"

    memory_clock_mhz = get_memory_clock_mhz(nvml, device)
    if memory_clock_mhz is None:
        mem_clock_text = "n/a"
    else:
        mem_clock_text = f"{memory_clock_mhz}MHz"

    voltage_uv = None
    if voltage_reader is not None:
        voltage_uv = voltage_reader.read_microvolts(device)
    if voltage_uv is None:
        voltage_text = "n/a"
    else:
        voltage_text = f"{voltage_uv / 1000.0:.0f}mV"

    clock_offset_text = format_clock_offsets(gpu_policy_controller)
    clock_ceiling_text = format_clock_ceiling_state(clock_ceiling_controller)
    vf_point_text = ""
    if vf_curve_reader is not None and graphics_clock_mhz is not None and voltage_uv is not None:
        try:
            vf_curve_reader.refresh_points()
        except Exception:
            pass
        vf_point_text = format_vf_curve_comparison(
            vf_curve_reader,
            graphics_clock_mhz,
            voltage_uv,
        )

    return (
        f"temp={current_temp_c:.1f}C "
        f"fan={fan_text} "
        f"power={power_text} "
        f"gpu_clock={clock_text} "
        f"mem_clock={mem_clock_text} "
        f"voltage={voltage_text} "
        f"{clock_ceiling_text}"
        f"{clock_offset_text}"
        f"{vf_point_text}"
    ).rstrip()


def run_nvidia_smi(args):
    try:
        result = subprocess.run(
            [NVIDIA_SMI, *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise NvmlError(f"{NVIDIA_SMI} not found") from exc

    output = result.stdout.strip() or result.stderr.strip()
    if result.returncode != 0:
        raise NvmlError(f"nvidia-smi {' '.join(args)} failed: {output or result.returncode}")

    return output


class FlattenedClockCeilingController:
    def __init__(self, flatten_target, policy_controller):
        if not flatten_target:
            raise ValueError("flatten_target is required")
        if policy_controller is None:
            raise ValueError("policy_controller is required")

        self._flatten_target = dict(flatten_target)
        self._policy_controller = policy_controller
        self._active = False
        self._range_lock = None

    @property
    def target_clock_mhz(self):
        return int(self._flatten_target["lock_clock_mhz"])

    @property
    def target_voltage_mv(self):
        voltage_mv = self._flatten_target.get("lock_voltage_mv")
        return int(voltage_mv) if voltage_mv is not None else None

    @property
    def requested_max_clock_mhz(self):
        if self._range_lock is None:
            return int(self.target_clock_mhz)
        return int(self._range_lock["requested_max_clock_mhz"])

    @property
    def applied_max_clock_mhz(self):
        if self._range_lock is None:
            return int(self.target_clock_mhz)
        return int(self._range_lock["applied_max_clock_mhz"])

    @property
    def applied_min_clock_mhz(self):
        if self._range_lock is None:
            return int(self.target_clock_mhz)
        return int(self._range_lock["applied_min_clock_mhz"])

    def telemetry_text(self):
        if not self._active:
            return ""

        ceiling_text = (
            f"{self.requested_max_clock_mhz}MHz"
            if self.requested_max_clock_mhz == self.applied_max_clock_mhz
            else f"{self.requested_max_clock_mhz}->{self.applied_max_clock_mhz}MHz"
        )
        voltage_mv = self.target_voltage_mv
        if voltage_mv is not None:
            ceiling_text += f"@{voltage_mv}mV"
        return f"clk_ceiling={ceiling_text} "

    def describe(self):
        snap_text = ""
        if self._range_lock is not None and self.requested_max_clock_mhz != self.applied_max_clock_mhz:
            snap_text = (
                f", supported-max={self.applied_max_clock_mhz}MHz"
                f" ({self._range_lock['max_mode']})"
            )
        min_text = (
            f", min-step={self.applied_min_clock_mhz}MHz"
            if self._range_lock is not None
            else ""
        )
        return (
            f"{describe_afterburner_dynamic_lock(self._flatten_target)}, "
            f"ceiling={self.requested_max_clock_mhz}MHz"
            f"{snap_text}{min_text}"
        )

    def apply(self):
        supported_steps = self._policy_controller.get_supported_graphics_clock_steps_mhz()
        requested_min_clock_mhz = supported_steps[0] if supported_steps else self.target_clock_mhz
        self._range_lock = self._policy_controller.apply_locked_graphics_clock_range_mhz(
            requested_min_clock_mhz,
            self.target_clock_mhz,
            prefer_max_not_above=True,
            snap_to_supported=True,
        )
        self._active = True
        return dict(self._range_lock)

    def close(self):
        if self._active:
            self._policy_controller.reset_locked_graphics_clocks()
            self._active = False


def apply_gpu_base_policy(gpu_index, enable_persistence_mode, power_limit_w):
    if enable_persistence_mode:
        output = run_nvidia_smi(["-pm", "1"])
        if output:
            log(output)

    if power_limit_w is not None:
        output = run_nvidia_smi(["-i", str(gpu_index), "-pl", str(power_limit_w)])
        if output:
            log(output)


def load_runtime_afterburner_fan_config(current_fan_config, *, afterburner_root, gpu_index):
    try:
        settings = load_afterburner_fan_settings(
            resolve_afterburner_fan_profile(afterburner_root=afterburner_root)
        )
    except Exception as exc:
        raise NvmlError(
            f"failed to read the imported Afterburner fan profile under {afterburner_root}: {exc}"
        ) from exc

    settings["afterburner_root"] = Path(afterburner_root).expanduser()
    if not settings["sw_auto_enabled"]:
        raise NvmlError("Afterburner software auto fan control is disabled in the imported profile")

    try:
        return build_imported_fan_section(
            current_fan_config,
            settings,
            gpu_index=gpu_index,
        )
    except SystemExit as exc:
        raise NvmlError(str(exc)) from None


def describe_current_gpu_policy_state(power_limits, clock_offsets):
    parts = []

    current_limit_w = power_limits.get("power_limit_w")
    default_limit_w = power_limits.get("power_limit_default_w")
    min_limit_w = power_limits.get("power_limit_min_w")
    max_limit_w = power_limits.get("power_limit_max_w")
    if current_limit_w is not None:
        power_text = f"power-limit={int(current_limit_w)}W"
        if default_limit_w is not None:
            power_text += f" default={int(default_limit_w)}W"
        if min_limit_w is not None and max_limit_w is not None:
            power_text += f" range={int(min_limit_w)}-{int(max_limit_w)}W"
        parts.append(power_text)

    mem_offset_mhz = clock_offsets.get("mem_clk_vf_offset_mhz")
    if mem_offset_mhz is not None:
        parts.append(f"mem-vf-offset={int(mem_offset_mhz):+d}MHz")

    return ", ".join(parts) if parts else "none"


def khz_to_mhz(value):
    if value is None:
        return None
    return int(round(float(value) / 1000.0))


def _chart_axis_bounds(values, *, rounding, include_zero=False):
    filtered = [float(value) for value in values if value is not None]
    if not filtered:
        lower = 0.0
        upper = float(rounding)
    else:
        lower = min(filtered)
        upper = max(filtered)
        if include_zero:
            lower = min(lower, 0.0)
            upper = max(upper, 0.0)
        lower = float(int(lower // rounding) * rounding)
        upper = float(int((upper + rounding - 1) // rounding) * rounding)
        if upper <= lower:
            upper = lower + float(rounding)
    return lower, upper


def _chart_merge_char(existing, new):
    if existing == " ":
        return new
    if existing == new:
        return existing
    if new == "@":
        return "@"
    if existing == "@":
        return "@"
    if existing == "#" or new == "#":
        return "#"
    return new


def _plot_chart_point(grid, row, col, char):
    if 0 <= row < len(grid) and 0 <= col < len(grid[row]):
        grid[row][col] = _chart_merge_char(grid[row][col], char)


def _map_chart_x(value, lower, upper, width):
    if width <= 1 or upper <= lower:
        return 0
    return int(round((float(value) - lower) * float(width - 1) / float(upper - lower)))


def _map_chart_y(value, lower, upper, height):
    if height <= 1 or upper <= lower:
        return 0
    scaled = (float(value) - lower) * float(height - 1) / float(upper - lower)
    return int(round(float(height - 1) - scaled))


def render_line_chart(
    title,
    *,
    series,
    x_label,
    y_label,
    x_rounding,
    y_rounding,
    width=56,
    height=12,
    include_zero_y=False,
    highlights=None,
):
    all_points = [
        (float(point[0]), float(point[1]))
        for item in series
        for point in item.get("points", [])
    ]
    if not all_points:
        return [f"{title}: unavailable"]

    x_values = [point[0] for point in all_points]
    y_values = [point[1] for point in all_points]
    x_lower, x_upper = _chart_axis_bounds(x_values, rounding=x_rounding)
    y_lower, y_upper = _chart_axis_bounds(
        y_values,
        rounding=y_rounding,
        include_zero=include_zero_y,
    )

    grid = [[" "] * int(width) for _ in range(int(height))]
    for item in series:
        points = [
            (float(point[0]), float(point[1]))
            for point in item.get("points", [])
        ]
        if not points:
            continue
        char = str(item.get("char", "#"))[:1] or "#"
        for left_point, right_point in zip(points, points[1:]):
            col0 = _map_chart_x(left_point[0], x_lower, x_upper, width)
            row0 = _map_chart_y(left_point[1], y_lower, y_upper, height)
            col1 = _map_chart_x(right_point[0], x_lower, x_upper, width)
            row1 = _map_chart_y(right_point[1], y_lower, y_upper, height)
            steps = max(abs(col1 - col0), abs(row1 - row0), 1)
            for step in range(steps + 1):
                col = int(round(col0 + (col1 - col0) * step / float(steps)))
                row = int(round(row0 + (row1 - row0) * step / float(steps)))
                _plot_chart_point(grid, row, col, char)
        for point in points:
            col = _map_chart_x(point[0], x_lower, x_upper, width)
            row = _map_chart_y(point[1], y_lower, y_upper, height)
            _plot_chart_point(grid, row, col, char)

    for highlight in highlights or []:
        x_value = highlight.get("x")
        y_value = highlight.get("y")
        if x_value is None or y_value is None:
            continue
        char = str(highlight.get("char", "@"))[:1] or "@"
        col = _map_chart_x(x_value, x_lower, x_upper, width)
        row = _map_chart_y(y_value, y_lower, y_upper, height)
        _plot_chart_point(grid, row, col, char)

    label_width = max(len(str(int(round(y_lower)))), len(str(int(round(y_upper)))), 3)
    tick_rows = {}
    tick_count = min(max(int(height // 2), 4), int(height))
    for index in range(tick_count):
        value = y_upper - (y_upper - y_lower) * float(index) / float(max(tick_count - 1, 1))
        value = round(value / float(y_rounding)) * float(y_rounding)
        row = _map_chart_y(value, y_lower, y_upper, height)
        tick_rows[row] = int(round(value))

    chart_lines = [title]
    for row_index, row_cells in enumerate(grid):
        if row_index in tick_rows:
            label = f"{tick_rows[row_index]:>{label_width}d}"
        else:
            label = " " * label_width
        chart_lines.append(f"{label} |{''.join(row_cells)}")
    chart_lines.append(f"{' ' * label_width} +{'-' * int(width)}")

    x_tick_count = 5
    x_tick_line = [" "] * int(width)
    for index in range(x_tick_count):
        value = x_lower + (x_upper - x_lower) * float(index) / float(max(x_tick_count - 1, 1))
        label = str(int(round(value)))
        col = int(round(index * float(width - 1) / float(max(x_tick_count - 1, 1))))
        start = max(0, min(int(width) - len(label), col - len(label) // 2))
        end = start + len(label)
        if any(x_tick_line[position] != " " for position in range(start, end)):
            continue
        for offset, char in enumerate(label):
            x_tick_line[start + offset] = char
    chart_lines.append(f"{' ' * label_width}  {''.join(x_tick_line)} {x_label}")
    return chart_lines


def format_dry_run_power_summary(translated_gpu_policy):
    parts = []

    power_limit_pct = translated_gpu_policy.get("power_limit_pct")
    source_w = translated_gpu_policy.get("power_limit_source_w")
    target_w = translated_gpu_policy.get("power_limit_w")
    cap_w = translated_gpu_policy.get("power_limit_cap_w")
    if power_limit_pct is not None and target_w is not None:
        power_text = f"AB {int(power_limit_pct)}% -> {int(target_w)}W"
        if source_w is not None and int(source_w) != int(target_w) and cap_w is not None:
            power_text += f" (manual cap, uncapped {int(source_w)}W)"
        parts.append(power_text)

    mem_offset_mhz = translated_gpu_policy.get("mem_clk_vf_offset_mhz")
    if mem_offset_mhz is not None:
        parts.append(f"memory target {int(mem_offset_mhz):+d}MHz")

    return ", ".join(parts) if parts else "none"


def format_dry_run_linux_state_summary(*, vf_changed_points, power_limits, clock_offsets):
    parts = []

    current_limit_w = power_limits.get("power_limit_w")
    if current_limit_w is not None:
        parts.append(f"current power {int(current_limit_w)}W")

    mem_offset_mhz = clock_offsets.get("mem_clk_vf_offset_mhz")
    if mem_offset_mhz is not None:
        parts.append(f"current memory {int(mem_offset_mhz):+d}MHz")

    if vf_changed_points is not None:
        parts.append(f"VF points changing {int(vf_changed_points)}")

    return ", ".join(parts) if parts else "none"


def run_afterburner_dry_run(
    *,
    config_path,
    fan_config,
    gpu_index,
    afterburner_runtime_options,
):
    afterburner_root = str(afterburner_runtime_options.get("afterburner_root", "")).strip()
    afterburner_profile = str(afterburner_runtime_options.get("afterburner_profile", "")).strip()
    afterburner_device_profile = str(
        afterburner_runtime_options.get("afterburner_device_profile", "")
    ).strip()
    power_limit_override_w = afterburner_runtime_options.get("power_limit_override_w")
    preserve_vanilla_below_mv = afterburner_runtime_options.get("preserve_vanilla_below_mv")
    dangerously_skip_validation = bool(
        afterburner_runtime_options.get("dangerously_skip_validation")
    )

    if not afterburner_root:
        raise NvmlError(
            "--dry-run requires --afterburner-dir or a configured afterburner_root in the runtime config"
        )

    afterburner_root = str(resolve_afterburner_root(afterburner_root))
    debug_log(
        f"dry-run-start gpu-index={gpu_index} config-path={config_path} "
        f"resolved-root={afterburner_root}"
    )
    emit_afterburner_debug_snapshot(
        afterburner_root=afterburner_root,
        requested_section=afterburner_profile,
        device_profile_hint=afterburner_device_profile,
        dangerously_skip_validation=dangerously_skip_validation,
    )

    policy_controller = None
    vf_curve_reader = None
    try:
        source = resolve_afterburner_vf_source(
            afterburner_root=afterburner_root,
            section=afterburner_profile or None,
            device_profile_hint=afterburner_device_profile or None,
            dangerously_skip_validation=dangerously_skip_validation,
        )
        debug_log(
            "dry-run-selected-source="
            f"profile={source['profile_path']} "
            f"section={source['section']} "
            f"skip-validation={source.get('dangerously_skip_validation')}"
        )
        section_info = source["section_info"]
        profile_settings = load_afterburner_profile_settings(
            profile_path=source["profile_path"],
            section=source["section"],
        )
        debug_log(
            "dry-run-selected-settings="
            f"{describe_afterburner_profile_settings(profile_settings)}"
        )
        flatten_target = section_info.get("flatten_target")

        fan_settings = load_afterburner_fan_settings(
            resolve_afterburner_fan_profile(afterburner_root=afterburner_root)
        )
        fan_settings["afterburner_root"] = Path(afterburner_root).expanduser()
        debug_log(
            "dry-run-fan-settings="
            f"period-ms={fan_settings['period_ms']} "
            f"flags=0x{int(fan_settings['flags_u32']):08x} "
            f"curve-points={len(fan_settings['curve']['points'])} "
            f"reference-points={len(fan_settings['curve2']['points'])}"
        )

        imported_fan_config = None
        imported_fan_error = None
        try:
            imported_fan_config = build_imported_fan_section(
                fan_config,
                fan_settings,
                gpu_index=gpu_index,
            )
        except SystemExit as exc:
            imported_fan_error = str(exc)
            debug_exception("failed to translate the imported fan curve", exc)

        power_limits = {}
        clock_offsets = {}
        policy_error = None
        translated_gpu_policy = translate_afterburner_gpu_policy(
            profile_settings,
            power_limits=power_limits,
            power_limit_cap_w=power_limit_override_w,
        )
        try:
            policy_controller = NvmlGpuPolicyController(gpu_index=gpu_index)
        except Exception as exc:
            policy_error = str(exc)
            debug_exception("failed to create the NVML GPU policy helper", exc)
        else:
            power_limits = policy_controller.query_power_limits()
            clock_offsets = policy_controller.get_clock_offsets()
            translated_gpu_policy = translate_afterburner_gpu_policy(
                profile_settings,
                power_limits=power_limits,
                power_limit_cap_w=power_limit_override_w,
            )
            debug_log(
                "dry-run-translated-policy="
                f"{describe_translated_gpu_policy(translated_gpu_policy)}"
            )

        vf_summary = None
        vf_plan = []
        missing_voltage_bins = []
        vf_curve_reader = create_hidden_vf_curve_reader(gpu_index=gpu_index)
        if vf_curve_reader is not None:
            vf_summary = vf_curve_reader.summary()
            debug_log(
                "linux-vf-summary="
                f"active-points={vf_summary['active_points']} "
                f"editable-core-points={vf_summary['editable_core_points']}"
            )
            vf_plan, missing_voltage_bins = build_plan(
                vf_curve_reader,
                section_info["materialization"]["points"],
                preserve_vanilla_below_mv=preserve_vanilla_below_mv,
            )
            debug_log(
                f"linux-vf-plan matched={len(vf_plan)} "
                f"missing-voltage-bins={len(missing_voltage_bins)}"
            )
        else:
            debug_log("linux-vf-summary=unavailable")

        changed_points = [
            item
            for item in vf_plan
            if int(item["current_offset_mhz"]) != int(item["new_offset_mhz"])
        ]
        if DEBUG_LOG_ENABLED and vf_plan:
            debug_log(
                f"linux-vf-point-detail count={len(vf_plan)} changed={len(changed_points)}"
            )
            for item in vf_plan:
                debug_log(
                    "linux-vf-point "
                    f"idx={int(item['index'])} "
                    f"mv={int(item['voltage_mv'])} "
                    f"base={int(item['base_mhz'])}MHz "
                    f"target={int(item['target_mhz'])}MHz "
                    f"current-offset={int(item['current_offset_mhz']):+d}MHz "
                    f"new-offset={int(item['new_offset_mhz']):+d}MHz "
                    f"preserve-vanilla={'yes' if item.get('preserve_vanilla') else 'no'}"
                )

        flags = fan_settings["flags"]
        lock_voltage_mv = flatten_target.get("lock_voltage_mv") if flatten_target else None
        end_voltage_mv = flatten_target.get("end_voltage_mv") if flatten_target else None
        lock_clock_mhz = flatten_target.get("lock_clock_mhz") if flatten_target else None
        source_label = f"{source['section']} in {source['profile_path'].name}"
        log(f"Dry run: {source_label}")
        log(f"Power and offsets: {format_dry_run_power_summary(translated_gpu_policy)}")
        if source.get("dangerously_skip_validation"):
            log(
                "Validation override: enabled. Skipping the usual flat-tail and "
                "undervolt checks against Defaults/Startup for profile selection."
            )
        if (
            flatten_target
            and lock_voltage_mv is not None
            and end_voltage_mv is not None
            and lock_clock_mhz is not None
        ):
            log(
                f"VF target: flat at {int(lock_clock_mhz)}MHz from "
                f"{int(lock_voltage_mv)}mV to {int(end_voltage_mv)}mV"
            )
        else:
            log(
                "VF target: "
                f"{describe_afterburner_vfcurve_analysis(section_info['analysis'])}"
            )
        if preserve_vanilla_below_mv is not None:
            log(
                "VF preserve: "
                f"keep the stock/base curve at and below {int(preserve_vanilla_below_mv)}mV"
            )

        validation = section_info.get("flatten_validation")
        if validation and validation.get("valid"):
            log(
                "Undervolt check: "
                f"{int(validation['selected_clock_mhz'])}MHz at "
                f"{int(validation['selected_voltage_mv'])}mV is "
                f"{int(round(float(validation['undervolt_margin_mv'])))}mV below "
                f"{validation['baseline_section']} at the same clock"
            )
        elif validation:
            if source.get("dangerously_skip_validation"):
                log(
                    "Undervolt check: skipped by user request; "
                    f"{describe_afterburner_flatten_validation(validation)}"
                )
            else:
                log(f"Undervolt check: {describe_afterburner_flatten_validation(validation)}")

        if imported_fan_config is None:
            log(f"Fan behavior: unavailable ({imported_fan_error})")
        else:
            fan_behavior_parts = [
                f"{float(fan_settings['period_ms']) / 1000.0:.1f}s updates",
                (
                    f"takeover {float(imported_fan_config['curve_manual_takeover_temp_c']):.0f}C"
                ),
                (
                    f"restore {float(imported_fan_config['curve_auto_restore_temp_c']):.0f}C"
                ),
                (
                    "emergency "
                    f"{float(imported_fan_config['emergency_auto_override_temp_c']):.0f}C/"
                    f"{float(imported_fan_config['emergency_auto_resume_temp_c']):.0f}C"
                ),
            ]
            if flags["override_zero_with_hardware_curve"]:
                fan_behavior_parts.append("zero-RPM preserved")
            log("Fan behavior: " + ", ".join(fan_behavior_parts))

        if policy_controller is None and vf_summary is None:
            log("Linux readback: unavailable")
        else:
            log(
                "Linux readback: "
                + format_dry_run_linux_state_summary(
                    vf_changed_points=len(changed_points) if vf_summary is not None else None,
                    power_limits=power_limits,
                    clock_offsets=clock_offsets,
                )
            )
            if policy_controller is None and policy_error:
                log(f"Linux power/memory readback note: {policy_error}")
            if vf_summary is None:
                log("Linux VF readback note: hidden NVAPI VF helper is unavailable")
                if preserve_vanilla_below_mv is not None:
                    log(
                        "Linux VF preserve note: "
                        "preserve-below-voltage needs Linux VF point data for an exact target preview"
                    )
            elif missing_voltage_bins:
                preview = ", ".join(
                    str(int(voltage_mv)) + "mV" for voltage_mv in missing_voltage_bins[:8]
                )
                if len(missing_voltage_bins) > 8:
                    preview += ", ..."
                log(f"Unmatched voltage bins: {preview}")

        if vf_summary is not None and vf_plan:
            vf_series = [
                {
                    "name": "stock",
                    "char": ".",
                    "points": sorted([
                        (
                            float(item["voltage_mv"]),
                            float(item["base_mhz"]),
                        )
                        for item in vf_plan
                    ]),
                },
                {
                    "name": "target",
                    "char": "#",
                    "points": sorted([
                        (float(item["voltage_mv"]), float(item["target_mhz"]))
                        for item in vf_plan
                    ]),
                },
            ]
            vf_title = "VF curve (target=# stock=. lock=@, x=mV y=MHz)"
        else:
            vf_series = [
                {
                    "name": "target",
                    "char": "#",
                    "points": sorted([
                        (
                            float(point["voltage_mv"]),
                            float(point["frequency_mhz"]),
                        )
                        for point in section_info["materialization"]["points"]
                    ]),
                }
            ]
            vf_title = "VF curve (target=# lock=@, x=mV y=MHz)"

        print(flush=True)
        for line in render_line_chart(
            vf_title,
            series=vf_series,
            x_label="mV",
            y_label="MHz",
            x_rounding=50,
            y_rounding=100,
            highlights=(
                [
                    {
                        "x": float(flatten_target["lock_voltage_mv"]),
                        "y": float(flatten_target["lock_clock_mhz"]),
                        "char": "@",
                    }
                ]
                if flatten_target
                and flatten_target.get("lock_voltage_mv") is not None
                and flatten_target.get("lock_clock_mhz") is not None
                else []
            ),
        ):
            log(line)

        fan_series = [
            {
                "name": "primary",
                "char": "#",
                "points": sorted([
                    (
                        float(point["temperature_c"]),
                        float(point["speed_pct"]),
                    )
                    for point in fan_settings["curve"]["points"]
                ]),
            },
            {
                "name": "reference",
                "char": ".",
                "points": sorted([
                    (
                        float(point["temperature_c"]),
                        float(point["speed_pct"]),
                    )
                    for point in fan_settings["curve2"]["points"]
                ]),
            },
        ]
        print(flush=True)
        for line in render_line_chart(
            "Fan curve (primary=# reference=., x=C y=%)",
            series=fan_series,
            x_label="C",
            y_label="%",
            x_rounding=10,
            y_rounding=10,
            include_zero_y=True,
        ):
            log(line)
    except Exception as exc:
        debug_exception("dry run failed", exc)
        raise
    finally:
        if vf_curve_reader is not None:
            vf_curve_reader.close()
        if policy_controller is not None:
            policy_controller.close()


def maybe_handle_first_time_afterburner_setup(
    *,
    argv,
    journal_hours,
    config_path,
    fan_config,
    gpu_index,
    afterburner_runtime_options,
):
    if (
        not sys.stdin.isatty()
        or not sys.stdout.isatty()
        or running_under_systemd_service()
    ):
        return False

    print(flush=True)
    log("First-time Afterburner import detected. Running a dry run before touching GPU state.")
    print(flush=True)
    script_path = launcher_script_path()
    try:
        run_afterburner_dry_run(
            config_path=config_path,
            fan_config=fan_config,
            gpu_index=gpu_index,
            afterburner_runtime_options=afterburner_runtime_options,
        )
    except Exception as exc:
        log(f"Dry run failed: {exc}")
        log("No GPU changes were applied.")
        log(
            "If the wrong saved Afterburner preset was auto-selected, re-run the dry run "
            "with an explicit section, for example:"
        )
        log(f"`{script_path} --dry-run --section Profile3`")
        return True
    print(flush=True)
    log("Dry run complete.")
    log(
        "Recommended next step: run PenguinBurner in foreground first so you can "
        "watch stdout logs and stop it with Ctrl-C."
    )
    if prompt_yes_no("Start PenguinBurner in foreground now for testing?", default=True):
        return False

    if systemd_is_available():
        if prompt_yes_no("Daemonize PenguinBurner under systemd now instead?", default=False):
            if os.geteuid() != 0:
                log(
                    "Daemon mode needs sudo. Re-run with "
                    f"`sudo {script_path} --daemonize` after you are happy with the dry run."
                )
                return True
            daemonize_with_systemd(argv, journal_hours=journal_hours)
            return True
    else:
        log("systemd background mode is unavailable on this system.")

    log("No GPU changes were applied.")
    log(f"When you are ready, run `{script_path}` for a foreground test.")
    if systemd_is_available():
        log(f"After that, you can daemonize it with `sudo {script_path} --daemonize`.")
    return True


def main(argv=None, *, journal_hours=DEFAULT_JOURNAL_HOURS):
    if argv is None:
        argv = sys.argv[1:]

    args = parse_main_args(argv)
    if args.debug_log:
        enable_debug_logging(args.config, argv=argv)
    config, config_path = load_config(args.config)
    gpu_config = config["gpu"]
    fan_config = config["fan"]
    if args.gpu_index is not None:
        gpu_config["index"] = int(args.gpu_index)
    gpu_index = int(gpu_config["index"])
    stored_afterburner_runtime_options = load_afterburner_runtime_options(config_path)
    had_persisted_afterburner_root = bool(
        str(stored_afterburner_runtime_options.get("afterburner_root", "")).strip()
    )
    afterburner_runtime_options = dict(stored_afterburner_runtime_options)
    if args.afterburner_dir.strip():
        afterburner_runtime_options["afterburner_root"] = str(
            resolve_afterburner_root(args.afterburner_dir)
        )
    if args.profile_section.strip():
        afterburner_runtime_options["afterburner_profile"] = str(args.profile_section).strip()
    if args.afterburner_device_profile.strip():
        afterburner_runtime_options["afterburner_device_profile"] = str(
            args.afterburner_device_profile
        ).strip()
    if args.power_limit_override_w is not None:
        afterburner_runtime_options["power_limit_override_w"] = (
            int(args.power_limit_override_w) if int(args.power_limit_override_w) > 0 else None
        )
    if args.preserve_vanilla_below_mv is not None:
        afterburner_runtime_options["preserve_vanilla_below_mv"] = (
            int(args.preserve_vanilla_below_mv)
            if int(args.preserve_vanilla_below_mv) > 0
            else None
        )
    if args.dangerously_skip_validation:
        afterburner_runtime_options["dangerously_skip_validation"] = True
    _debug_log_effective_runtime_options(
        config_path=config_path,
        gpu_index=gpu_index,
        afterburner_runtime_options=afterburner_runtime_options,
    )

    if args.dry_run:
        run_afterburner_dry_run(
            config_path=config_path,
            fan_config=fan_config,
            gpu_index=gpu_index,
            afterburner_runtime_options=afterburner_runtime_options,
        )
        return

    afterburner_root = str(afterburner_runtime_options.get("afterburner_root", "")).strip()
    afterburner_profile = str(afterburner_runtime_options.get("afterburner_profile", "")).strip()
    afterburner_device_profile = str(
        afterburner_runtime_options.get("afterburner_device_profile", "")
    ).strip()
    if not had_persisted_afterburner_root:
        afterburner_runtime_options = ensure_afterburner_root_configured(
            config_path,
            afterburner_runtime_options,
            gpu_index=gpu_index,
            interactive=sys.stdin.isatty(),
        )
        afterburner_root = str(afterburner_runtime_options.get("afterburner_root", "")).strip()
        afterburner_profile = str(afterburner_runtime_options.get("afterburner_profile", "")).strip()
        afterburner_device_profile = str(
            afterburner_runtime_options.get("afterburner_device_profile", "")
        ).strip()
        if afterburner_root and maybe_handle_first_time_afterburner_setup(
            argv=argv,
            journal_hours=journal_hours,
            config_path=config_path,
            fan_config=fan_config,
            gpu_index=gpu_index,
            afterburner_runtime_options=afterburner_runtime_options,
        ):
            return
    elif not afterburner_root:
        afterburner_runtime_options = ensure_afterburner_root_configured(
            config_path,
            afterburner_runtime_options,
            gpu_index=gpu_index,
            interactive=sys.stdin.isatty(),
        )
        afterburner_root = str(afterburner_runtime_options.get("afterburner_root", "")).strip()
        afterburner_profile = str(afterburner_runtime_options.get("afterburner_profile", "")).strip()
        afterburner_device_profile = str(
            afterburner_runtime_options.get("afterburner_device_profile", "")
        ).strip()
    if afterburner_root:
        fan_config = load_runtime_afterburner_fan_config(
            fan_config,
            afterburner_root=afterburner_root,
            gpu_index=gpu_index,
        )

    nvml = ctypes.CDLL("libnvidia-ml.so.1")

    c_uint = ctypes.c_uint
    c_void_p = ctypes.c_void_p

    nvml.nvmlInit_v2.restype = ctypes.c_int
    nvml.nvmlShutdown.restype = ctypes.c_int
    nvml.nvmlDeviceGetHandleByIndex_v2.argtypes = [c_uint, ctypes.POINTER(c_void_p)]
    nvml.nvmlDeviceGetHandleByIndex_v2.restype = ctypes.c_int
    nvml.nvmlDeviceGetTemperature.argtypes = [c_void_p, c_uint, ctypes.POINTER(c_uint)]
    nvml.nvmlDeviceGetTemperature.restype = ctypes.c_int
    nvml.nvmlDeviceGetNumFans.argtypes = [c_void_p, ctypes.POINTER(c_uint)]
    nvml.nvmlDeviceGetNumFans.restype = ctypes.c_int
    nvml.nvmlDeviceGetFanSpeed.argtypes = [c_void_p, ctypes.POINTER(c_uint)]
    nvml.nvmlDeviceGetFanSpeed.restype = ctypes.c_int
    nvml.nvmlDeviceGetFanSpeed_v2.argtypes = [c_void_p, c_uint, ctypes.POINTER(c_uint)]
    nvml.nvmlDeviceGetFanSpeed_v2.restype = ctypes.c_int
    nvml.nvmlDeviceGetPowerUsage.argtypes = [c_void_p, ctypes.POINTER(c_uint)]
    nvml.nvmlDeviceGetPowerUsage.restype = ctypes.c_int
    nvml.nvmlDeviceGetClockInfo.argtypes = [c_void_p, c_uint, ctypes.POINTER(c_uint)]
    nvml.nvmlDeviceGetClockInfo.restype = ctypes.c_int
    if hasattr(nvml, "nvmlDeviceGetMinMaxFanSpeed"):
        nvml.nvmlDeviceGetMinMaxFanSpeed.argtypes = [
            c_void_p,
            ctypes.POINTER(c_uint),
            ctypes.POINTER(c_uint),
        ]
        nvml.nvmlDeviceGetMinMaxFanSpeed.restype = ctypes.c_int
    nvml.nvmlDeviceSetFanSpeed_v2.argtypes = [c_void_p, c_uint, c_uint]
    nvml.nvmlDeviceSetFanSpeed_v2.restype = ctypes.c_int
    nvml.nvmlDeviceSetDefaultFanSpeed_v2.argtypes = [c_void_p, c_uint]
    nvml.nvmlDeviceSetDefaultFanSpeed_v2.restype = ctypes.c_int

    curve = [tuple(point) for point in fan_config["curve"]]

    validate_curve(curve)

    gpu_index = gpu_config["index"]
    poll_interval_s = fan_config["poll_interval_s"]
    hysteresis_c = fan_config["hysteresis_c"]
    mode = fan_config["mode"]
    min_fan_speed_pct = fan_config["min_fan_speed_pct"]
    max_fan_speed_pct = fan_config["max_fan_speed_pct"]
    max_step_up_pct_per_s = fan_config["max_step_up_pct_per_s"]
    max_step_down_pct_per_s = fan_config["max_step_down_pct_per_s"]
    manual_enable_temp_c = fan_config["manual_enable_temp_c"]
    auto_restore_temp_c = fan_config["auto_restore_temp_c"]
    emergency_auto_override_temp_c = fan_config.get("emergency_auto_override_temp_c", 80.0)
    emergency_auto_resume_temp_c = fan_config.get("emergency_auto_resume_temp_c", 75.0)
    enable_persistence_mode = gpu_config["enable_persistence_mode"]
    force_update_every_poll = fan_config["force_update_every_poll"]
    translated_gpu_policy = None
    afterburner_source = None
    afterburner_profile_settings = None
    vf_apply_result = None
    clock_ceiling_controller = None
    vf_expected_samples = []
    last_vf_reapply_monotonic = 0.0
    vf_reapply_cooldown_s = max(float(poll_interval_s), 10.0)

    device = c_void_p()
    check(nvml.nvmlInit_v2(), "nvmlInit_v2")
    check(
        nvml.nvmlDeviceGetHandleByIndex_v2(c_uint(gpu_index), ctypes.byref(device)),
        "nvmlDeviceGetHandleByIndex_v2",
    )
    voltage_reader = create_hidden_voltage_reader(nvml)
    vf_curve_reader = create_hidden_vf_curve_reader(gpu_index=gpu_index)
    try:
        gpu_policy_controller = NvmlGpuPolicyController(gpu_index=gpu_index)
    except Exception as exc:
        gpu_policy_controller = None
        log(f"Linux GPU policy helper unavailable: {exc}")

    if afterburner_root:
        try:
            afterburner_source = resolve_afterburner_vf_source(
                afterburner_root=afterburner_root,
                section=afterburner_profile or None,
                device_profile_hint=afterburner_device_profile or None,
                dangerously_skip_validation=bool(
                    afterburner_runtime_options.get("dangerously_skip_validation")
                ),
            )
        except Exception as exc:
            log(f"Skipping Afterburner source resolve: error={exc}")
        else:
            if afterburner_source.get("dangerously_skip_validation"):
                log(
                    "Afterburner validation override enabled: skipping the default "
                    "flat-tail and undervolt checks for the saved profile."
                )
            if gpu_policy_controller is not None:
                try:
                    afterburner_profile_settings = load_afterburner_profile_settings(
                        profile_path=afterburner_source["profile_path"],
                        section=afterburner_source["section"],
                    )
                    translated_gpu_policy = translate_afterburner_gpu_policy(
                        afterburner_profile_settings,
                        power_limits=gpu_policy_controller.query_power_limits(),
                        power_limit_cap_w=afterburner_runtime_options["power_limit_override_w"],
                    )
                except Exception as exc:
                    translated_gpu_policy = None
                    log(
                        "Skipping Afterburner GPU policy translate: "
                        f"section={afterburner_source['section']} error={exc}"
                    )

    startup_power_limit_w = None
    if translated_gpu_policy is not None and translated_gpu_policy.get("power_limit_w") is not None:
        startup_power_limit_w = translated_gpu_policy["power_limit_w"]
    apply_gpu_base_policy(
        gpu_index=gpu_index,
        enable_persistence_mode=enable_persistence_mode,
        power_limit_w=startup_power_limit_w,
    )
    if vf_curve_reader is not None and afterburner_source is not None:
            if translated_gpu_policy is not None and gpu_policy_controller is not None:
                try:
                    apply_translated_gpu_policy(gpu_policy_controller, translated_gpu_policy)
                except Exception as exc:
                    log(
                        "Skipping Afterburner GPU policy apply: "
                        f"section={afterburner_source['section']} error={exc}"
                    )
                else:
                    log(
                        f"Applied Afterburner GPU policy: section={afterburner_source['section']} "
                        f"{describe_translated_gpu_policy(translated_gpu_policy)}."
                    )
            try:
                vf_apply_result = apply_afterburner_curve_to_reader(
                    vf_curve_reader,
                    profile_path=afterburner_source["profile_path"],
                    section=afterburner_source["section"],
                    gpu_policy=translated_gpu_policy,
                    preserve_vanilla_below_mv=afterburner_runtime_options["preserve_vanilla_below_mv"],
                )
            except Exception as exc:
                log(
                    "Skipping Afterburner VF curve apply: "
                    f"section={afterburner_source['section']} error={exc}"
                )
            else:
                log(
                    f"Applied Afterburner VF curve: section={afterburner_source['section']} "
                    f"matched={len(vf_apply_result['plan'])} "
                    f"changed={len(vf_apply_result['changed_points'])} "
                    f"mode={vf_apply_result['translation_mode']} "
                    f"origin={vf_apply_result['translation_origin']} "
                    f"linux_profile={vf_apply_result['translated_linux_profile_path']}."
                )
                vf_expected_samples = select_expected_vf_samples(vf_apply_result["plan"])
                flatten_target = derive_afterburner_dynamic_lock(vf_apply_result["materialization"]["points"])
                if flatten_target is None:
                    log(
                        f"Skipping Afterburner clock ceiling: section={afterburner_source['section']} "
                        "no flattened V/F target was detected."
                    )
                else:
                    try:
                        clock_ceiling_controller = FlattenedClockCeilingController(
                            flatten_target=flatten_target,
                            policy_controller=gpu_policy_controller,
                        )
                        clock_ceiling_controller.apply()
                    except Exception as exc:
                        clock_ceiling_controller = None
                        log(
                            "Skipping Afterburner clock ceiling: "
                            f"section={afterburner_source['section']} error={exc}"
                        )
                    else:
                        log(
                            f"Configured Afterburner clock ceiling: section={afterburner_source['section']} "
                            f"{clock_ceiling_controller.describe()}."
                        )

    fan_count = c_uint()
    check(nvml.nvmlDeviceGetNumFans(device, ctypes.byref(fan_count)), "nvmlDeviceGetNumFans")

    if fan_count.value == 0:
        raise NvmlError("GPU reports zero controllable fans")

    device_min_fan_speed_pct = None
    device_max_fan_speed_pct = None
    if hasattr(nvml, "nvmlDeviceGetMinMaxFanSpeed"):
        fan_min = c_uint()
        fan_max = c_uint()
        rc = nvml.nvmlDeviceGetMinMaxFanSpeed(
            device,
            ctypes.byref(fan_min),
            ctypes.byref(fan_max),
        )
        if rc == NVML_SUCCESS and fan_max.value >= fan_min.value:
            device_min_fan_speed_pct = fan_min.value
            device_max_fan_speed_pct = fan_max.value

    effective_min_fan_speed_pct = min_fan_speed_pct
    effective_max_fan_speed_pct = max_fan_speed_pct
    if device_min_fan_speed_pct is not None:
        effective_min_fan_speed_pct = max(effective_min_fan_speed_pct, device_min_fan_speed_pct)
    if device_max_fan_speed_pct is not None:
        effective_max_fan_speed_pct = min(effective_max_fan_speed_pct, device_max_fan_speed_pct)
    if effective_max_fan_speed_pct < effective_min_fan_speed_pct:
        raise NvmlError("effective fan speed range is invalid")

    restored = False

    def restore_default():
        nonlocal restored
        if restored:
            return
        restored = True
        for fan_idx in range(fan_count.value):
            nvml.nvmlDeviceSetDefaultFanSpeed_v2(device, c_uint(fan_idx))
        if clock_ceiling_controller is not None:
            clock_ceiling_controller.close()
        if vf_curve_reader is not None:
            vf_curve_reader.close()
        if gpu_policy_controller is not None:
            gpu_policy_controller.close()
        nvml.nvmlShutdown()

    def stop(_signum, _frame):
        restore_default()
        sys.exit(0)

    atexit.register(restore_default)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    last_speed = None
    last_set_temp_c = None
    last_update_time = time.monotonic()
    manual_mode_active = False
    hot_auto_mode_active = False
    print(
        f"Controlling GPU {gpu_index} with {fan_count.value} fan(s), "
        f"mode={mode}, hysteresis={hysteresis_c} C, "
        f"manual-limits={effective_min_fan_speed_pct}-{effective_max_fan_speed_pct}%, "
        f"manual-enable={manual_enable_temp_c} C, auto-restore={auto_restore_temp_c} C, "
        f"emergency-auto={emergency_auto_override_temp_c} C/{emergency_auto_resume_temp_c} C. "
        "Press Ctrl-C to restore auto mode."
    , flush=True)
    startup_gpu_policy = translated_gpu_policy or {"power_limit_w": startup_power_limit_w}
    log(
        f"GPU policy: persistence={'on' if enable_persistence_mode else 'off'}, "
        f"{describe_translated_gpu_policy(startup_gpu_policy)}."
    )
    log(f"Config file: {config_path}")
    if afterburner_source is not None:
        flatten_target = afterburner_source["section_info"].get("flatten_target")
        flatten_text = (
            describe_afterburner_dynamic_lock(flatten_target)
            if flatten_target is not None
            else "none"
        )
        log(
            "Afterburner import: "
            f"root={afterburner_source['afterburner_root']} "
            f"device_profile={afterburner_source['profile_path'].name} "
            f"profile={afterburner_source['section']} "
            f"flatten-target={flatten_text}."
        )
        log(
            "Afterburner flatten validation: "
            f"{describe_afterburner_flatten_validation(afterburner_source['section_info'].get('flatten_validation'))}."
        )
        if afterburner_profile_settings is not None:
            log(
                "Afterburner parsed settings: "
                f"{describe_afterburner_profile_settings(afterburner_profile_settings)}."
            )
    if vf_curve_reader is not None:
        vf_summary = vf_curve_reader.summary()
        log(
            f"Linux NVAPI VF curve: "
            f"active-points={vf_summary['active_points']}, "
            f"editable-core-points={vf_summary['editable_core_points']}."
        )
    if device_min_fan_speed_pct is not None and device_max_fan_speed_pct is not None:
        log(
            f"Device fan limits reported by NVML: "
            f"{device_min_fan_speed_pct}-{device_max_fan_speed_pct}%."
        )
    effective_manual_curve = build_effective_manual_curve(
        curve=curve,
        manual_enable_temp_c=manual_enable_temp_c,
        effective_min_fan_speed_pct=effective_min_fan_speed_pct,
        effective_max_fan_speed_pct=effective_max_fan_speed_pct,
        mode=mode,
    )
    curve_source = fan_config.get("curve_source")
    if curve_source:
        curve_flags_u32 = int(fan_config.get("curve_source_flags_u32", 0))
        curve_period_ms = int(fan_config.get("curve_source_period_ms", int(round(poll_interval_s * 1000))))
        log(
            f"Fan curve source: {curve_source} "
            f"period={curve_period_ms}ms flags=0x{curve_flags_u32:08x}."
        )
    log(f"Fan curve points: {format_curve_points(curve)}")
    log(f"Effective manual fan curve: {format_curve_points(effective_manual_curve)}")
    if fan_config.get("curve_override_zero_with_hardware_curve"):
        behavior_parts = ["zero-rpm zone uses hardware auto curve"]
        if fan_config.get("curve_hardware_auto_below_device_min"):
            behavior_parts.append(
                "below device manual minimum uses hardware auto curve"
            )
        takeover_temp_c = fan_config.get("curve_manual_takeover_temp_c")
        if takeover_temp_c is not None:
            behavior_parts.append(f"manual takeover near {float(takeover_temp_c):.2f}C")
        log("Fan curve behavior: " + "; ".join(behavior_parts) + ".")
    log(
        "Silent fan curve guardrail: "
        f"hardware auto above {float(emergency_auto_override_temp_c):.0f}C, "
        f"resume manual below {float(emergency_auto_resume_temp_c):.0f}C."
    )
    if clock_ceiling_controller is not None:
        log(f"Clock ceiling policy: {clock_ceiling_controller.describe()}.")

    while True:
        loop_started = time.monotonic()
        temp = c_uint()
        check(
            nvml.nvmlDeviceGetTemperature(device, c_uint(NVML_TEMPERATURE_GPU), ctypes.byref(temp)),
            "nvmlDeviceGetTemperature",
        )

        current_temp_c = float(temp.value)
        power_draw_w = get_power_draw_w(nvml, device)

        telemetry_text = format_telemetry(
            nvml,
            device,
            fan_count.value,
            current_temp_c,
            voltage_reader=voltage_reader,
            vf_curve_reader=vf_curve_reader,
            gpu_policy_controller=gpu_policy_controller,
            power_draw_w=power_draw_w,
            clock_ceiling_controller=clock_ceiling_controller,
        )
        if vf_curve_reader is not None and vf_expected_samples:
            vf_mismatches = detect_vf_curve_reset(vf_curve_reader, vf_expected_samples)
            if vf_mismatches and (loop_started - last_vf_reapply_monotonic) >= vf_reapply_cooldown_s:
                try:
                    apply_plan(vf_curve_reader, vf_apply_result["plan"])
                    vf_curve_reader.refresh_points()
                except Exception as exc:
                    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                    log(f"{timestamp} event=vf-curve-reapply-error error={exc}")
                else:
                    last_vf_reapply_monotonic = loop_started
                    mismatch_preview = ", ".join(
                        (
                            f"{int(item['voltage_mv'])}mV:"
                            f"{int(item['current_offset_mhz']):+d}->"
                            f"{int(item['expected_offset_mhz']):+d}MHz"
                        )
                        for item in vf_mismatches[:4]
                    )
                    if len(vf_mismatches) > 4:
                        mismatch_preview += ", ..."
                    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                    log(
                        f"{timestamp} {telemetry_text} "
                        f"event=vf-curve-reapplied mismatches={len(vf_mismatches)} "
                        f"samples={mismatch_preview}"
                    )
        fan_curve_state_text = describe_fan_curve_state(
            current_temp_c=current_temp_c,
            effective_curve=effective_manual_curve,
            manual_mode_active=manual_mode_active,
            emergency_auto_mode_active=hot_auto_mode_active,
            emergency_auto_resume_temp_c=emergency_auto_resume_temp_c,
        )
        if hot_auto_mode_active and current_temp_c > emergency_auto_resume_temp_c:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            log(f"{timestamp} {telemetry_text} {fan_curve_state_text} fan_mode=auto reason=emergency-override")
            time.sleep(poll_interval_s)
            continue

        if hot_auto_mode_active and current_temp_c <= emergency_auto_resume_temp_c:
            hot_auto_mode_active = False
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            fan_curve_state_text = describe_fan_curve_state(
                current_temp_c=current_temp_c,
                effective_curve=effective_manual_curve,
                manual_mode_active=manual_mode_active,
                emergency_auto_mode_active=hot_auto_mode_active,
                emergency_auto_resume_temp_c=emergency_auto_resume_temp_c,
            )
            log(f"{timestamp} {telemetry_text} {fan_curve_state_text} event=emergency-override-cleared")

        if current_temp_c > emergency_auto_override_temp_c:
            if manual_mode_active:
                for fan_idx in range(fan_count.value):
                    check(
                        nvml.nvmlDeviceSetDefaultFanSpeed_v2(device, c_uint(fan_idx)),
                        f"nvmlDeviceSetDefaultFanSpeed_v2 fan {fan_idx}",
                    )
                manual_mode_active = False
                last_speed = None
                last_set_temp_c = None
            hot_auto_mode_active = True
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            fan_curve_state_text = describe_fan_curve_state(
                current_temp_c=current_temp_c,
                effective_curve=effective_manual_curve,
                manual_mode_active=manual_mode_active,
                emergency_auto_mode_active=hot_auto_mode_active,
                emergency_auto_resume_temp_c=emergency_auto_resume_temp_c,
            )
            log(
                f"{timestamp} {telemetry_text} {fan_curve_state_text} "
                f"event=restoring-auto-mode reason=emergency-override"
            )
            time.sleep(poll_interval_s)
            continue

        if not manual_mode_active and current_temp_c < manual_enable_temp_c:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            log(f"{timestamp} {telemetry_text} {fan_curve_state_text} fan_mode=auto")
            time.sleep(poll_interval_s)
            continue

        if not manual_mode_active:
            manual_mode_active = True
            last_speed = None
            last_set_temp_c = None
            last_update_time = loop_started
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            fan_curve_state_text = describe_fan_curve_state(
                current_temp_c=current_temp_c,
                effective_curve=effective_manual_curve,
                manual_mode_active=manual_mode_active,
                emergency_auto_mode_active=hot_auto_mode_active,
                emergency_auto_resume_temp_c=emergency_auto_resume_temp_c,
            )
            log(f"{timestamp} {telemetry_text} {fan_curve_state_text} event=entering-manual-mode")

        if current_temp_c <= auto_restore_temp_c:
            for fan_idx in range(fan_count.value):
                check(
                    nvml.nvmlDeviceSetDefaultFanSpeed_v2(device, c_uint(fan_idx)),
                    f"nvmlDeviceSetDefaultFanSpeed_v2 fan {fan_idx}",
                )
            manual_mode_active = False
            last_speed = None
            last_set_temp_c = None
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            fan_curve_state_text = describe_fan_curve_state(
                current_temp_c=current_temp_c,
                effective_curve=effective_manual_curve,
                manual_mode_active=manual_mode_active,
                emergency_auto_mode_active=hot_auto_mode_active,
                emergency_auto_resume_temp_c=emergency_auto_resume_temp_c,
            )
            log(f"{timestamp} {telemetry_text} {fan_curve_state_text} event=restoring-auto-mode")
            time.sleep(poll_interval_s)
            continue

        raw_target_speed = speed_for_temp(current_temp_c, curve, mode=mode)
        raw_target_speed = clamp(
            raw_target_speed,
            effective_min_fan_speed_pct,
            effective_max_fan_speed_pct,
        )

        hysteresis_target_speed = apply_hysteresis(
            current_temp_c=current_temp_c,
            raw_target_speed=raw_target_speed,
            last_temp_c=last_set_temp_c,
            last_speed=last_speed,
            hysteresis_c=hysteresis_c,
        )

        limited_target_speed = limit_speed_change(
            target_speed=hysteresis_target_speed,
            last_speed=last_speed,
            elapsed_s=loop_started - last_update_time,
            max_step_up_pct_per_s=max_step_up_pct_per_s,
            max_step_down_pct_per_s=max_step_down_pct_per_s,
        )
        target_speed = round(
            clamp(
                limited_target_speed,
                effective_min_fan_speed_pct,
                effective_max_fan_speed_pct,
            )
        )

        if force_update_every_poll or target_speed != last_speed:
            for fan_idx in range(fan_count.value):
                check(
                    nvml.nvmlDeviceSetFanSpeed_v2(device, c_uint(fan_idx), c_uint(target_speed)),
                    f"nvmlDeviceSetFanSpeed_v2 fan {fan_idx}",
                )
            last_set_temp_c = current_temp_c
            last_speed = target_speed
            last_update_time = loop_started

        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        log(
            f"{timestamp} {telemetry_text} {fan_curve_state_text} "
            f"target={target_speed}% curve={raw_target_speed:.1f}% "
            f"hyst={hysteresis_target_speed:.1f}% fan_mode=manual"
        )

        time.sleep(poll_interval_s)


if __name__ == "__main__":
    try:
        runtime_flags = parse_runtime_flags(sys.argv[1:])
        runtime_argv = runtime_flags["passthrough"]
        if runtime_flags["install_systemd_service"]:
            install_systemd_service(
                runtime_argv,
                journal_hours=runtime_flags["journal_hours"],
            )
        elif runtime_flags["uninstall_systemd_service"]:
            uninstall_systemd_service()
        elif runtime_flags["daemonize"] and not runtime_flags["foreground"] and not running_under_systemd_service():
            daemonize_with_systemd(
                runtime_argv,
                journal_hours=runtime_flags["journal_hours"],
            )
        else:
            main(
                runtime_argv,
                journal_hours=runtime_flags["journal_hours"],
            )
    except Exception as exc:
        debug_exception("fatal error", exc)
        print(f"error: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
