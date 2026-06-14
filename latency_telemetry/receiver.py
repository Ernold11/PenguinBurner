from __future__ import annotations

from collections import deque
import json
import math
import os
import pwd
from pathlib import Path
import select
import socket
import threading
import time
from typing import Callable

from common.penguin_burner_paths import claim_desktop_user_ownership

from .layer_check import DEFAULT_LATENCY_LAYER_LAUNCH_OPTIONS

MARKER_INPUT_SAMPLE_BIT = 1 << 6
RAW_TIMING_LOG_ENV = "PENGUIN_BURNER_LATENCY_RAW_TIMING_LOG"
# Opt-in (via --dump-latency-data or this env) verbose latency dump: present
# mode / queue depth on swapchain creation plus Reflex sleep-mode (boost / FPS
# cap) and recovery transitions. Off by default; debugging display/VRR and
# frame-gen behaviour only.
DUMP_LATENCY_DATA_ENV = "PENGUIN_BURNER_DUMP_LATENCY_DATA"
DXVK_NVAPI_VKREFLEX_SOURCE = "dxvk-nvapi-vkreflex"

QUALITY_RANK = {
    "none": 0,
    "present-frametime": 1,
    "base-frame-marker": 2,
    "driver-timing": 2,
    "reflex-marker-presence": 2,
    "reflex-marker-simulation": 2,
    "reflex-markers": 2,
    "reflex-marker-render-submit": 3,
    "reflex-marker-sim-present": 3,
    "reflex-marker-submit-present": 3,
    # An input-sample anchor is strictly earlier and more complete than a
    # sim-start anchor, so the input tier outranks the sim tier.
    "reflex-marker-input-present": 4,
    "reflex-render-submit": 3,
    "reflex-simulation": 3,
    "reflex-input-present": 5,
}

# Latency tier names published as snapshot latency_quality. The cross-phase
# span fields originate in the C++ layer (marker_timing_quality /
# send_marker_timing_sample in native/latency_layer/src/telemetry_events.cpp);
# keep the vocabularies in sync.
LATENCY_TIER_INPUT_PRESENT = "input-to-present"
LATENCY_TIER_MARKER_INPUT_PRESENT = "marker-input-to-present"
LATENCY_TIER_INPUT_OOB_PRESENT = "input-to-oob-present"
LATENCY_TIER_SIM_PRESENT = "sim-to-present"
LATENCY_TIER_SIM_OOB_PRESENT = "sim-to-oob-present"
LATENCY_TIER_SUBMIT_PRESENT = "submit-to-present"
LATENCY_TIER_RENDER_SUBMIT = "render-submit"

METER_LOG_INTERVAL_S = 3.0
METER_SAMPLE_MAX_AGE_S = 3.0
METER_MAX_SAMPLES = 4096
STALE_DRIVER_REPORT_MAX_AGE_S = 30.0
METER_MIN_PRESENT_SAMPLES = 4
METER_UNSEEDED_PRESENT_FPS_MAX = 70.0
METER_MARKER_FPS_MAX_PRESENT_RATIO = 1.25
BASE_FRAME_MARKER_PRIORITY = (
    "oob-present-start",
    "present-start",
    "rendersubmit-start",
)
FRAMEGEN_ACTIVE_KEYS = ("framegen_active", "frame_generation_active", "fg_active")
FRAMEGEN_MARKER_KEYS = ("framegen_frame", "generated_frame", "fg_frame")
FRAMEGEN_COUNT_KEYS = (
    "framegen_frame_count",
    "generated_frame_count",
    "fg_frame_count",
)
FRAMEGEN_MEASUREMENTS = {"framegen-marker", "framegen-frame", "generated-frame"}
# Output (present) cadence must exceed the base render cadence by at least this
# ratio before the overlay reports frame generation. Out-of-band present markers
# and the sim->displayed-present span are emitted by Reflex low-latency mode with
# frame generation OFF, so marker presence alone is not evidence of generated
# frames -- only a multiplied display rate is.
FRAMEGEN_CADENCE_RATIO = 1.5
FRAMEGEN_MARKER_NAMES = {
    "oob-present-start",
    "oob-present-end",
    "out-of-band-present-start",
    "out-of-band-present-end",
    "out_of_band_present_start",
    "out_of_band_present_end",
}


def latency_socket_path(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    explicit = str(env.get("PENGUIN_BURNER_LATENCY_SOCKET") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    runtime_dir = str(env.get("XDG_RUNTIME_DIR") or "").strip()
    if runtime_dir:
        return Path(runtime_dir) / "penguin-burner" / "latency.sock"
    if os.getuid() == 0:
        sudo_uid = str(env.get("SUDO_UID") or "").strip()
        if sudo_uid.isdigit():
            candidate = Path("/run/user") / sudo_uid
            if candidate.exists():
                return candidate / "penguin-burner" / "latency.sock"
        sudo_user = str(env.get("SUDO_USER") or "").strip()
        if sudo_user:
            try:
                candidate = Path("/run/user") / str(pwd.getpwnam(sudo_user).pw_uid)
            except KeyError:
                candidate = Path()
            if candidate.exists():
                return candidate / "penguin-burner" / "latency.sock"
    return Path(f"/tmp/penguin-burner-latency-{os.getuid()}.sock")


def _home_latency_socket_path(env: dict[str, str]) -> Path | None:
    home = str(env.get("HOME") or "").strip()
    if home and home != "/root":
        return Path(home).expanduser() / ".cache" / "penguin-burner" / "latency.sock"

    sudo_uid = str(env.get("SUDO_UID") or "").strip()
    if sudo_uid.isdigit():
        try:
            user_home = pwd.getpwuid(int(sudo_uid)).pw_dir
        except KeyError:
            user_home = ""
        if user_home:
            return Path(user_home) / ".cache" / "penguin-burner" / "latency.sock"

    sudo_user = str(env.get("SUDO_USER") or "").strip()
    if sudo_user:
        try:
            user_home = pwd.getpwnam(sudo_user).pw_dir
        except KeyError:
            user_home = ""
        if user_home:
            return Path(user_home) / ".cache" / "penguin-burner" / "latency.sock"

    candidates: list[Path] = []
    try:
        candidates.append(Path.cwd())
    except OSError:
        pass
    try:
        candidates.append(Path(__file__).resolve())
    except OSError:
        pass
    for base in candidates:
        for candidate in (base, *base.parents):
            if candidate.parent == Path("/home"):
                return candidate / ".cache" / "penguin-burner" / "latency.sock"
    return None


def latency_socket_paths(env: dict[str, str] | None = None) -> list[Path]:
    env = os.environ if env is None else env
    paths = [latency_socket_path(env)]
    if not str(env.get("PENGUIN_BURNER_LATENCY_SOCKET") or "").strip():
        home_path = _home_latency_socket_path(env)
        if home_path is not None:
            paths.append(home_path)

    unique_paths: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key not in seen:
            unique_paths.append(path)
            seen.add(key)
    return unique_paths


def _p95_us(values: list[int]) -> int | None:
    values = sorted(value for value in values if value > 0)
    if not values:
        return None
    index = max(0, min(len(values) - 1, int(round((len(values) - 1) * 0.95))))
    return values[index]


def _median_us(values: list[int]) -> int | None:
    values = sorted(value for value in values if value > 0)
    if not values:
        return None
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return int(round((values[mid - 1] + values[mid]) / 2.0))


def _format_ms(value_us: int | None) -> str:
    if value_us is None:
        return "n/a"
    return f"{value_us / 1000.0:.2f}ms"


def _format_fps_value(fps: float | None) -> str:
    if not fps or fps <= 0:
        return "n/a"
    return f"{math.floor(fps + 0.5)}"


def _format_fps(frametime_us: int | None) -> str:
    if not frametime_us:
        return "n/a"
    return _format_fps_value(1_000_000 / frametime_us)


def _fps_from_frametime(frametime_us: int | None) -> float | None:
    if not frametime_us:
        return None
    return 1_000_000 / frametime_us


def _mean_fps(frametime_values_us: list[int]) -> float | None:
    frametimes = [value for value in frametime_values_us if value > 0]
    if not frametimes:
        return None
    return len(frametimes) * 1_000_000 / sum(frametimes)


def _empty_present_fps_stats() -> dict[str, str]:
    return {
        "p95": "n/a",
        "avg": "n/a",
        "median": "n/a",
        "5pct_low": "n/a",
        "1pct_low": "n/a",
    }


def _present_fps_stats(frametime_values_us: list[int]) -> dict[str, str]:
    frametimes = [value for value in frametime_values_us if value > 0]
    if len(frametimes) < METER_MIN_PRESENT_SAMPLES:
        return _empty_present_fps_stats()

    def low_fps(percent: float) -> str:
        slow_frame_count = max(1, math.ceil(len(frametimes) * percent))
        slow_frametimes = sorted(frametimes, reverse=True)[:slow_frame_count]
        slow_mean_frametime_us = sum(slow_frametimes) / slow_frame_count
        return _format_fps_value(1_000_000 / slow_mean_frametime_us)

    return {
        "p95": _format_fps(_p95_us(frametimes)),
        "avg": _format_fps_value(len(frametimes) * 1_000_000 / sum(frametimes)),
        "median": _format_fps(_median_us(frametimes)),
        "5pct_low": low_fps(0.05),
        "1pct_low": low_fps(0.01),
    }


def _select_base_marker_frametime_p95(
    samples: list[dict],
    *,
    raw_avg_fps: float | None,
) -> int | None:
    marker_frametimes: dict[str, list[int]] = {}
    for sample in samples:
        frametime_us = _int_value(sample.get("base_frame_frametime_us"))
        if frametime_us <= 0:
            continue
        marker_name = str(sample.get("marker_name") or "unknown")
        marker_frametimes.setdefault(marker_name, []).append(frametime_us)

    def valid_p95(frametimes: list[int]) -> int | None:
        if len(frametimes) < METER_MIN_PRESENT_SAMPLES:
            return None
        p95_us = _p95_us(frametimes)
        marker_fps = _fps_from_frametime(p95_us)
        if (
            marker_fps is not None
            and raw_avg_fps is not None
            and marker_fps > raw_avg_fps * METER_MARKER_FPS_MAX_PRESENT_RATIO
        ):
            return None
        return p95_us

    for marker_name in BASE_FRAME_MARKER_PRIORITY:
        p95_us = valid_p95(marker_frametimes.get(marker_name, []))
        if p95_us is not None:
            return p95_us

    eligible_sources = [
        (marker_name, frametimes, p95_us)
        for marker_name, frametimes in marker_frametimes.items()
        if (p95_us := valid_p95(frametimes)) is not None
    ]
    if not eligible_sources:
        return None
    eligible_sources.sort(key=lambda source: (-len(source[1]), source[0]))
    return eligible_sources[0][2]


def _int_value(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _truthy_value(value: object) -> bool:
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "on", "active"}


def _explicit_framegen_active(samples: list[dict]) -> bool:
    for sample in samples:
        if any(_truthy_value(sample.get(key)) for key in FRAMEGEN_ACTIVE_KEYS):
            return True
        if any(_truthy_value(sample.get(key)) for key in FRAMEGEN_MARKER_KEYS):
            return True
        if any(_int_value(sample.get(key)) > 0 for key in FRAMEGEN_COUNT_KEYS):
            return True
        marker_name = str(sample.get("marker_name") or "").strip().lower()
        if marker_name in FRAMEGEN_MARKER_NAMES:
            return True
        if _positive_us(sample, "sim_to_oob_present_us") or _positive_us(
            sample, "input_to_oob_present_us"
        ):
            return True
        measurement = str(sample.get("measurement") or "").strip().lower()
        if measurement in FRAMEGEN_MEASUREMENTS:
            return True
    return False


def _framegen_active_for_overlay(
    samples: list[dict],
    *,
    base_fps: float | None,
    raw_output_fps: float | None,
    cadence_is_independent: bool,
) -> bool:
    """Whether to report frame generation on the overlay.

    Frame generation is confirmed only when the displayed (present) cadence
    clearly exceeds the base render cadence -- i.e. extra frames really are being
    produced. Reflex out-of-band present markers and the sim->displayed-present
    span are deliberately NOT treated as evidence here: they are emitted with
    frame generation off, which is what made the overlay show a phantom "FG" rate.
    Source-asserted generated-frame evidence (an explicit ``framegen_active``
    flag, generated-frame markers/counts, or a generated-frame measurement) is
    still trusted.

    The cadence-ratio test only counts when ``cadence_is_independent`` -- i.e. the
    base FPS came from a real base-render signal (frame markers) or was
    deinterlaced from a previously established steady base. When the base is just
    the current window's p95 (the slow-frame tail) of the present stream itself,
    a high mean-vs-p95 ratio is intra-window frametime variance (a stuttery,
    uncapped game), NOT generated frames, so it must not trip frame generation.
    """
    base = _safe_float(base_fps)
    output = _safe_float(raw_output_fps)
    if (
        cadence_is_independent
        and base
        and output
        and base > 0
        and output >= base * FRAMEGEN_CADENCE_RATIO
    ):
        return True

    for sample in samples:
        if any(_truthy_value(sample.get(key)) for key in FRAMEGEN_ACTIVE_KEYS):
            return True
        if any(_truthy_value(sample.get(key)) for key in FRAMEGEN_MARKER_KEYS):
            return True
        if any(_int_value(sample.get(key)) > 0 for key in FRAMEGEN_COUNT_KEYS):
            return True
        measurement = str(sample.get("measurement") or "").strip().lower()
        if measurement in FRAMEGEN_MEASUREMENTS:
            return True
    return False


def _has_marker_stream(samples: list[dict]) -> bool:
    """Whether the window carries a Reflex/nvapi marker stream.

    Real frame generation always arrives with the marker stream: the nvapi
    bridge emits marker-proxy / out-of-band-present samples carrying
    ``marker_bits`` and a sim->present span, and base-render frames carry
    ``base_frame_frametime_us``. A title with no Reflex integration (e.g. a
    simple game) produces only the Vulkan layer's plain present-pacing
    frametimes -- no markers at all. Without any marker stream a present-rate
    increase is a genuine framerate change (a 30fps menu -> 120fps gameplay),
    not generated frames, so it must not be deinterlaced into a phantom base.
    """
    for sample in samples:
        if _int_value(sample.get("marker_bits")):
            return True
        if str(sample.get("measurement") or "") == "marker-proxy":
            return True
        if _int_value(sample.get("base_frame_frametime_us")) > 0:
            return True
        if _positive_us(sample, "sim_to_present_us") or _positive_us(
            sample, "sim_to_oob_present_us"
        ):
            return True
    return False


def _safe_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _dump_latency_data_enabled(env: dict[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    value = str(env.get(DUMP_LATENCY_DATA_ENV) or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _raw_timing_log_interval(env: dict[str, str] | None = None) -> float | None:
    env = os.environ if env is None else env
    value = str(env.get(RAW_TIMING_LOG_ENV) or "").strip()
    if not value:
        return None
    if value.lower() in {"0", "false", "no", "off"}:
        return None
    if value.lower() in {"all", "always"}:
        return 0.0
    try:
        return max(0.0, float(value))
    except ValueError:
        return 1.0


def _positive_us(sample: dict, key: str) -> bool:
    return _int_value(sample.get(key)) > 0


def _elapsed_us_from_sample(sample: dict, start_key: str, end_key: str) -> int:
    start_us = _int_value(sample.get(start_key))
    end_us = _int_value(sample.get(end_key))
    if not start_us or not end_us or end_us <= start_us:
        return 0
    return end_us - start_us


def _looks_like_driver_timing_report(sample: dict) -> bool:
    if _positive_us(sample, "timing_count"):
        return True
    return any(
        _positive_us(sample, key)
        for key in (
            "driver_start_us",
            "driver_end_us",
            "gpu_render_start_us",
            "gpu_render_end_us",
        )
    )


def normalize_timing_sample(sample: dict) -> dict:
    if sample.get("type") != "timing":
        return sample

    normalized = dict(sample)
    if (
        not str(normalized.get("measurement") or "")
        and normalized.get("source") == DXVK_NVAPI_VKREFLEX_SOURCE
        and _looks_like_driver_timing_report(normalized)
    ):
        normalized["measurement"] = "driver-report"

    if not _positive_us(normalized, "gpu_render_us"):
        gpu_render_us = _elapsed_us_from_sample(
            normalized, "gpu_render_start_us", "gpu_render_end_us"
        )
        if gpu_render_us:
            normalized["gpu_render_us"] = gpu_render_us

    if not _positive_us(normalized, "render_present_us"):
        render_present_us = _elapsed_us_from_sample(
            normalized, "present_start_us", "gpu_render_end_us"
        )
        if render_present_us:
            normalized["render_present_us"] = render_present_us

    return normalized


def _quality_for_sample(sample: dict) -> str:
    is_marker_proxy = str(sample.get("measurement") or "") == "marker-proxy"
    if is_marker_proxy:
        if _positive_us(sample, "input_to_present_us"):
            return "reflex-marker-input-present"
        if _positive_us(sample, "sim_to_present_us") or _positive_us(
            sample, "sim_to_oob_present_us"
        ):
            return "reflex-marker-sim-present"
        if _positive_us(sample, "submit_to_present_us"):
            return "reflex-marker-submit-present"
        if _positive_us(sample, "render_submit_us"):
            return "reflex-marker-render-submit"
        if _positive_us(sample, "sim_us"):
            return "reflex-marker-simulation"
        if _int_value(sample.get("marker_bits")):
            return "reflex-marker-presence"

    if _positive_us(sample, "input_to_present_us"):
        return "reflex-input-present"

    quality = str(sample.get("quality") or "none")
    if _positive_us(sample, "render_submit_us") and quality.startswith("reflex"):
        return "reflex-render-submit"
    if quality == "reflex-markers" and _int_value(sample.get("marker_bits")):
        return "reflex-marker-presence"
    return quality


def _missing_metric_hints(
    samples: list[dict],
    *,
    input_present_p95: int | None,
    gpu_frame_p95: int | None,
) -> list[str]:
    hints: list[str] = []
    has_low_latency_functions = any(
        bool(sample.get("vk_nv_low_latency2_functions")) for sample in samples
    )
    has_marker_activity = any(_int_value(sample.get("marker_bits")) for sample in samples)
    has_input_marker = any(
        _int_value(sample.get("marker_bits")) & MARKER_INPUT_SAMPLE_BIT
        for sample in samples
    )
    has_driver_timestamps = any(
        _positive_us(sample, key)
        for sample in samples
        for key in (
            "gpu_render_start_us",
            "gpu_render_end_us",
            "driver_start_us",
            "driver_end_us",
        )
    )

    if input_present_p95 is None:
        if has_marker_activity and not has_input_marker:
            hints.append("input-sample")
        elif has_low_latency_functions:
            hints.append("input-present")
    if gpu_frame_p95 is None:
        if has_low_latency_functions and not has_driver_timestamps:
            hints.append("driver-timing")
        elif has_low_latency_functions:
            hints.append("gpu-frame-delta")
    return hints


def _latency_proxy_p95(samples: list[dict]) -> tuple[int | None, str | None]:
    def _tier_p95(key: str, *, marker_proxy: bool | None = None) -> int | None:
        return _p95_us(
            [
                _int_value(sample.get(key))
                for sample in samples
                if marker_proxy is None
                or (str(sample.get("measurement") or "") == "marker-proxy")
                == marker_proxy
            ]
        )

    # When frame generation is active, the sim->out-of-band-present span is the
    # closest available proxy to click-to-photon: it runs to the actual display
    # hand-off after the generated-frame pacing hold. The narrower sim/input->
    # app-present spans end at the application present and miss that hold, so they
    # read unrealistically low (e.g. ~18 ms at a 40 fps base under frame gen).
    # Prefer the wider displayed-present span here.
    if _explicit_framegen_active(samples):
        # Widest displayed-present span first: input->display when the title emits
        # INPUT_SAMPLE, else sim->display. Both include the frame-generation hold.
        for key, tier in (
            ("input_to_oob_present_us", LATENCY_TIER_INPUT_OOB_PRESENT),
            ("sim_to_oob_present_us", LATENCY_TIER_SIM_OOB_PRESENT),
        ):
            oob_p95 = _tier_p95(key)
            if oob_p95 is not None:
                return oob_p95, tier

    tiers = (
        ("input_to_present_us", False, LATENCY_TIER_INPUT_PRESENT),
        ("input_to_present_us", True, LATENCY_TIER_MARKER_INPUT_PRESENT),
        ("sim_to_present_us", None, LATENCY_TIER_SIM_PRESENT),
        ("sim_to_oob_present_us", None, LATENCY_TIER_SIM_OOB_PRESENT),
        ("submit_to_present_us", None, LATENCY_TIER_SUBMIT_PRESENT),
        ("render_submit_us", None, LATENCY_TIER_RENDER_SUBMIT),
    )
    for key, marker_proxy, tier in tiers:
        p95 = _tier_p95(key, marker_proxy=marker_proxy)
        if p95 is not None:
            return p95, tier
    return None, None


class LatencyTelemetryMeter:
    def __init__(
        self,
        *,
        max_samples: int = METER_MAX_SAMPLES,
        max_sample_age_s: float = METER_SAMPLE_MAX_AGE_S,
        stale_driver_report_max_age_s: float = STALE_DRIVER_REPORT_MAX_AGE_S,
        time_monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._samples = deque(maxlen=max_samples)
        self._max_sample_age_s = float(max_sample_age_s)
        self._stale_driver_report_max_age_s = float(stale_driver_report_max_age_s)
        self._time_monotonic = time_monotonic
        self._last_base_present_fps: float | None = None
        self._last_ignored_driver_report: dict | None = None
        self._driver_report_present_ids: dict[tuple[object, ...], tuple[int, int]] = {}

    def add_sample(self, sample: dict) -> dict | None:
        if sample.get("type") != "timing":
            return None
        stored = normalize_timing_sample(sample)
        stored["_received_monotonic"] = self._time_monotonic()
        if str(stored.get("measurement") or "") == "driver-report":
            self._synthesize_driver_report_duplicate_count(stored)
        if (
            str(stored.get("measurement") or "") == "driver-report"
            and _int_value(stored.get("driver_report_duplicate_count")) > 0
        ):
            self._last_ignored_driver_report = stored
            return stored
        if str(stored.get("measurement") or "") == "driver-report":
            self._last_ignored_driver_report = None
        self._samples.append(stored)
        return stored

    # Reflex timing rings hold up to 64 reports; a stale ring re-presented in
    # any order cycles within this span, so a presentID at or below the max we
    # have seen — but within the ring span — is a repeat, not fresh data.
    _DRIVER_REPORT_RING_SPAN = 256

    def _synthesize_driver_report_duplicate_count(self, sample: dict) -> None:
        present_id = _int_value(sample.get("present_id"))
        if not present_id:
            sample.setdefault("driver_report_duplicate_count", 0)
            return

        key = (
            sample.get("pid"),
            sample.get("source"),
            sample.get("device"),
            sample.get("swapchain"),
        )
        max_present_id, duplicate_count = self._driver_report_present_ids.get(
            key, (0, 0)
        )
        if (
            max_present_id
            and present_id <= max_present_id
            and max_present_id - present_id <= self._DRIVER_REPORT_RING_SPAN
        ):
            duplicate_count += 1
        else:
            # Fresh report, or a genuine presentID reset (e.g. swapchain
            # recreation) far below the ring span.
            duplicate_count = 0
            max_present_id = present_id
        self._driver_report_present_ids[key] = (max_present_id, duplicate_count)
        # The layer's own counter resets whenever stale IDs cycle, so trust
        # whichever side counted more repeats (observed live on 2026-06-11).
        provided = _int_value(sample.get("driver_report_duplicate_count"))
        sample["driver_report_duplicate_count"] = max(provided, duplicate_count)

    def summary(self, *, now: float | None = None) -> str | None:
        snapshot = self.snapshot(now=now)
        if not snapshot:
            return self._stale_driver_report_summary(now=now)
        samples = snapshot["samples"]
        now = float(snapshot["now"])
        latest = samples[-1]
        best_quality = snapshot["best_quality"]
        stale_driver_report = self._fresh_stale_driver_report(now=now)
        has_fresh_reflex_or_driver_sample = any(
            str(sample.get("measurement") or "") != "present-pacing"
            for sample in samples
        )
        if stale_driver_report is not None and not has_fresh_reflex_or_driver_sample:
            best_quality = "stale-driver-report"
        gpu_frame_p95 = snapshot["gpu_frame_p95_us"]
        input_present_p95 = snapshot["input_present_p95_us"]
        render_submit_p95 = snapshot["render_submit_p95_us"]
        render_present_p95 = snapshot["render_present_p95_us"]
        gpu_render_p95 = snapshot["gpu_render_p95_us"]
        present_frametime_p95 = snapshot["present_frametime_p95_us"]
        latency_proxy_p95 = snapshot["latency_proxy_p95_us"]
        present_fps = snapshot["present_fps"]
        present_fps_stats = snapshot["raw_present_fps_stats"]
        has_rich_latency_sample = any(
            str(sample.get("measurement") or "")
            not in {"present-pacing", "base-frame-marker-pacing"}
            for sample in samples
        )
        fps_tail = (
            f"present-fps={present_fps} "
            f"raw-present-fps-avg={present_fps_stats['avg']} "
            f"raw-present-fps-median={present_fps_stats['median']} "
            f"raw-present-fps-5pct-low={present_fps_stats['5pct_low']} "
            f"raw-present-fps-1pct-low={present_fps_stats['1pct_low']}"
        )
        if not has_rich_latency_sample and stale_driver_report is None:
            return (
                f"event=latency-meter pid={latest.get('pid', 'unknown')} "
                f"quality={best_quality} samples={len(samples)} "
                f"present-frametime-p95={_format_ms(present_frametime_p95)} "
                f"{fps_tail}"
            )

        missing_hints = _missing_metric_hints(
            samples,
            input_present_p95=input_present_p95,
            gpu_frame_p95=gpu_frame_p95,
        )
        missing_text = (
            f" missing={','.join(missing_hints)}" if missing_hints else ""
        )
        stale_text = ""
        if stale_driver_report is not None and not has_fresh_reflex_or_driver_sample:
            stale_text = (
                f" stale-present_id={stale_driver_report.get('present_id', 'unknown')}"
                " stale-driver-report-duplicates="
                f"{_int_value(stale_driver_report.get('driver_report_duplicate_count'))}"
                f" stale-age-s={self._stale_driver_report_age_s(stale_driver_report, now=now):.1f}"
            )
        return (
            f"event=latency-meter pid={latest.get('pid', 'unknown')} "
            f"quality={best_quality} samples={len(samples)} "
            f"latency-proxy-p95={_format_ms(latency_proxy_p95)} "
            f"latency-quality={snapshot['latency_quality'] or 'n/a'} "
            f"sim-to-present-p95={_format_ms(snapshot['sim_to_present_p95_us'])} "
            f"submit-to-present-p95="
            f"{_format_ms(snapshot['submit_to_present_p95_us'])} "
            f"render-submit-p95={_format_ms(render_submit_p95)} "
            f"render-present-p95={_format_ms(render_present_p95)} "
            f"gpu-render-p95={_format_ms(gpu_render_p95)} "
            f"input-present-p95={_format_ms(input_present_p95)} "
            f"gpu-frame-p95={_format_ms(gpu_frame_p95)} "
            f"display-latency-p95={_format_ms(snapshot['display_latency_p95_us'])} "
            f"present-frametime-p95={_format_ms(present_frametime_p95)} "
            f"{fps_tail}"
            f"{stale_text}"
            f"{missing_text}"
        )

    def snapshot(self, *, now: float | None = None) -> dict | None:
        if not self._samples:
            return None
        now = self._time_monotonic() if now is None else now
        samples = [
            sample
            for sample in self._samples
            if now - float(sample.get("_received_monotonic") or 0.0)
            <= self._max_sample_age_s
        ]
        if not samples:
            return None
        best_quality = max(
            (_quality_for_sample(sample) for sample in samples),
            key=lambda quality: QUALITY_RANK.get(quality, 0),
        )
        present_frametime_values = [
            _int_value(sample.get("present_frametime_us")) for sample in samples
        ]
        present_frametimes = [
            value for value in present_frametime_values if value > 0
        ]
        present_frametime_p95 = (
            _p95_us(present_frametimes)
            if len(present_frametimes) >= METER_MIN_PRESENT_SAMPLES
            else None
        )
        present_median = _median_us(present_frametime_values)
        present_fps_stats = _present_fps_stats(present_frametime_values)
        raw_present_avg_fps = _mean_fps(present_frametimes)
        marker_frametime_p95 = _select_base_marker_frametime_p95(
            samples,
            raw_avg_fps=raw_present_avg_fps,
        )
        if marker_frametime_p95 is not None:
            base_present_frametime_p95 = marker_frametime_p95
            present_fps_value = _fps_from_frametime(marker_frametime_p95)
            self._last_base_present_fps = present_fps_value
            cadence_is_independent = True
        else:
            present_fps_value, cadence_is_independent = (
                self._estimate_base_present_fps(
                    p95_fps=_fps_from_frametime(present_frametime_p95),
                    raw_avg_fps=raw_present_avg_fps,
                    marker_stream=_has_marker_stream(samples),
                )
            )
            base_present_frametime_p95 = (
                int(round(1_000_000 / present_fps_value))
                if present_fps_value
                else None
            )
        framegen_active = _framegen_active_for_overlay(
            samples,
            base_fps=present_fps_value,
            raw_output_fps=raw_present_avg_fps,
            cadence_is_independent=cadence_is_independent,
        )
        latency_proxy_p95, latency_quality = _latency_proxy_p95(samples)
        return {
            "now": float(now),
            "samples": samples,
            "best_quality": best_quality,
            "latency_proxy_p95_us": latency_proxy_p95,
            "latency_p95_ms": (
                None
                if latency_proxy_p95 is None
                else float(latency_proxy_p95) / 1000.0
            ),
            "latency_quality": latency_quality,
            "sim_to_present_p95_us": _p95_us(
                [_int_value(sample.get("sim_to_present_us")) for sample in samples]
            ),
            "submit_to_present_p95_us": _p95_us(
                [
                    _int_value(sample.get("submit_to_present_us"))
                    for sample in samples
                ]
            ),
            "render_submit_p95_us": _p95_us(
                [_int_value(sample.get("render_submit_us")) for sample in samples]
            ),
            "render_present_p95_us": _p95_us(
                [_int_value(sample.get("render_present_us")) for sample in samples]
            ),
            "gpu_render_p95_us": _p95_us(
                [_int_value(sample.get("gpu_render_us")) for sample in samples]
            ),
            "input_present_p95_us": _p95_us(
                [_int_value(sample.get("input_to_present_us")) for sample in samples]
            ),
            "gpu_frame_p95_us": _p95_us(
                [_int_value(sample.get("gpu_frame_time_us")) for sample in samples]
            ),
            # Present->scanout tail measured by the layer via VK_KHR_present_wait
            # (measurement="display-latency"). Kept as its own metric, separate
            # from latency_proxy_p95; the overlay sums them into one number.
            "display_latency_p95_us": (
                display_latency_p95 := _p95_us(
                    [
                        _int_value(sample.get("display_latency_us"))
                        for sample in samples
                    ]
                )
            ),
            "display_latency_p95_ms": (
                None
                if display_latency_p95 is None
                else float(display_latency_p95) / 1000.0
            ),
            "present_frametime_p95_us": present_frametime_p95,
            "present_frametime_p95_ms": (
                None
                if present_frametime_p95 is None
                else float(present_frametime_p95) / 1000.0
            ),
            "present_fps": _format_fps_value(present_fps_value),
            "present_fps_value": present_fps_value,
            "base_present_fps": present_fps_value,
            "base_present_frametime_p95_us": base_present_frametime_p95,
            "base_present_frametime_p95_ms": (
                None
                if base_present_frametime_p95 is None
                else float(base_present_frametime_p95) / 1000.0
            ),
            "raw_present_fps_stats": present_fps_stats,
            "raw_present_fps_avg": raw_present_avg_fps,
            "raw_present_fps_median": _format_fps(present_median),
            "framegen_active": framegen_active,
        }

    def _stale_driver_report_age_s(self, latest: dict, *, now: float) -> float:
        return now - float(latest.get("_received_monotonic") or 0.0)

    def _fresh_stale_driver_report(self, *, now: float) -> dict | None:
        latest = self._last_ignored_driver_report
        if latest is None:
            return None
        age_s = self._stale_driver_report_age_s(latest, now=now)
        if age_s > self._stale_driver_report_max_age_s:
            return None
        return latest

    def _stale_driver_report_summary(self, *, now: float | None = None) -> str | None:
        now = self._time_monotonic() if now is None else now
        latest = self._fresh_stale_driver_report(now=now)
        if latest is None:
            return None
        age_s = self._stale_driver_report_age_s(latest, now=now)
        return (
            f"event=latency-meter pid={latest.get('pid', 'unknown')} "
            "quality=stale-driver-report samples=0 "
            "latency-proxy-p95=n/a render-submit-p95=n/a "
            "render-present-p95=n/a gpu-render-p95=n/a "
            "input-present-p95=n/a gpu-frame-p95=n/a "
            "present-frametime-p95=n/a present-fps=n/a "
            f"stale-present_id={latest.get('present_id', 'unknown')} "
            "stale-driver-report-duplicates="
            f"{_int_value(latest.get('driver_report_duplicate_count'))} "
            f"stale-age-s={age_s:.1f} missing=fresh-samples"
        )

    def _estimate_base_present_fps(
        self,
        *,
        p95_fps: float | None,
        raw_avg_fps: float | None,
        marker_stream: bool,
    ) -> tuple[float | None, bool]:
        """Return ``(base_fps, deinterlaced)``.

        ``deinterlaced`` is True only when the base was recovered by dividing a
        steady high present rate against the previously established base because
        frame generation turned on. That division is gated on ``marker_stream``:
        without a Reflex/nvapi marker stream a multiplied present rate is a real
        framerate increase (e.g. a 30fps menu -> 120fps gameplay), so dividing it
        down against a stale base would invent a phantom base + frame-gen rate.
        When the division is not taken the base is just the current window's p95.
        """
        if p95_fps is None:
            return None, False

        last_base = self._last_base_present_fps
        if last_base is None and p95_fps > METER_UNSEEDED_PRESENT_FPS_MAX:
            return None, False

        inferred_multiplier = self._infer_framegen_multiplier(raw_avg_fps, last_base)

        base_fps = p95_fps
        deinterlaced = False
        if (
            marker_stream
            and last_base is not None
            and p95_fps > last_base * 1.6
            and raw_avg_fps
        ):
            multiplier = inferred_multiplier
            if multiplier is not None:
                base_fps = raw_avg_fps / multiplier
                deinterlaced = True

        self._last_base_present_fps = base_fps
        return base_fps, deinterlaced

    @staticmethod
    def _infer_framegen_multiplier(
        raw_avg_fps: float | None,
        last_base_fps: float | None,
    ) -> int | None:
        if not raw_avg_fps or not last_base_fps:
            return None
        ratio = raw_avg_fps / last_base_fps
        nearest = int(round(ratio))
        if nearest not in {2, 3, 4}:
            return None
        if abs(ratio - nearest) > 0.45:
            return None
        return nearest


class LatencyTelemetryLogger:
    def __init__(
        self,
        *,
        path: Path | None = None,
        paths: list[Path] | None = None,
        log: Callable[[str], None],
        log_interval_s: float = METER_LOG_INTERVAL_S,
        raw_log_interval_s: float | None = None,
        dump_latency_data: bool | None = None,
        time_monotonic: Callable[[], float] = time.monotonic,
        time_strftime: Callable[[str], str] = time.strftime,
    ) -> None:
        if paths is not None:
            self.paths = paths
        elif path is not None:
            self.paths = [path]
        else:
            self.paths = latency_socket_paths()
        self.path = self.paths[0]
        self.log = log
        self.log_interval_s = float(log_interval_s)
        self.raw_log_interval_s = (
            _raw_timing_log_interval()
            if raw_log_interval_s is None
            else raw_log_interval_s
        )
        self._dump_latency_data = (
            _dump_latency_data_enabled()
            if dump_latency_data is None
            else bool(dump_latency_data)
        )
        self.time_monotonic = time_monotonic
        self.time_strftime = time_strftime
        self.meter = LatencyTelemetryMeter(time_monotonic=time_monotonic)
        self._socket: socket.socket | None = None
        self._sockets: list[tuple[socket.socket, Path]] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_log_monotonic = 0.0
        self._last_raw_log_monotonic = 0.0
        # Reflex sleep-mode (boost/cap) is set per-frame; remember the last logged
        # signature per swapchain so we only log on change instead of every frame.
        self._last_sleep_mode_sig: dict[object, tuple] = {}

    def start(self) -> "LatencyTelemetryLogger":
        for path in self.paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            claim_desktop_user_ownership(path.parent, include_parents=True)
            if path.exists():
                path.unlink()
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            sock.setblocking(False)
            sock.bind(str(path))
            try:
                path.chmod(0o666)
            except OSError:
                pass
            claim_desktop_user_ownership(path)
            self._sockets.append((sock, path))
        self._socket = self._sockets[0][0]
        for path in self.paths:
            self.log(f"Latency telemetry socket: {path}")
        self.log(
            "Latency telemetry launch options: "
            f"{DEFAULT_LATENCY_LAYER_LAUNCH_OPTIONS}"
        )
        self._thread = threading.Thread(
            target=self._run,
            name="penguin-burner-latency-telemetry",
            daemon=True,
        )
        self._thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._socket is not None:
            self._socket = None
        for sock, _path in self._sockets:
            sock.close()
        self._sockets = []
        try:
            for path in self.paths:
                if path.exists():
                    path.unlink()
        except OSError:
            pass

    def _run(self) -> None:
        while not self._stop.is_set():
            sockets = [sock for sock, _path in self._sockets]
            if not sockets:
                return
            readable, _writable, _errors = select.select(sockets, [], [], 0.5)
            for sock in readable:
                self._receive_available(sock)
            self._maybe_log_summary()

    def _receive_available(self, sock: socket.socket) -> None:
        while True:
            try:
                payload = sock.recv(8192)
            except BlockingIOError:
                return
            except OSError:
                return
            try:
                sample = json.loads(payload.decode("utf-8", errors="replace"))
            except Exception:
                continue
            if not isinstance(sample, dict):
                continue
            sample_type = sample.get("type")
            if sample_type == "status":
                self._maybe_log_status(sample)
                continue
            if sample_type != "timing":
                continue
            stored_sample = self.meter.add_sample(normalize_timing_sample(sample))
            if stored_sample is not None:
                self._maybe_log_raw_timing(stored_sample)

    def _maybe_log_summary(self) -> None:
        now = self.time_monotonic()
        if now - self._last_log_monotonic < self.log_interval_s:
            return
        summary = self.meter.summary(now=now)
        if not summary:
            return
        self._last_log_monotonic = now
        self.log(f"{self.time_strftime('%Y-%m-%d %H:%M:%S')} {summary}")

    # Layer status events worth surfacing. The present-flow snapshot is the only
    # carrier of the VK_KHR_present_wait / present_id probe fields, so logging it
    # is how the display-latency feasibility gate becomes visible.
    _STATUS_LOG_EVENTS = (
        "present-flow",
        "create-device",
        "create-swapchain",
        "present-id-inject",
        "present-wait-result",
    )

    # Extra events surfaced only under --dump-latency-data: Reflex sleep-mode
    # (boost / FPS-cap) + recovery transitions. Logged on change only (see
    # _sleep_mode_is_duplicate) since the set event fires per-frame. These expose
    # low_latency_boost and minimum_interval_us, the closest readouts to
    # Reflex-Boost and the VRR frame-rate-cap internals.
    _DUMP_STATUS_LOG_EVENTS = (
        "latency-sleep-mode-set",
        "latency-recovery-unavailable",
        "latency-recovery-disable-sleep-mode",
        "latency-recovery-reset-sleep-mode-enter",
        "latency-recovery-reset-sleep-mode",
        "latency-recovery-reapply-sleep-mode",
    )

    _STATUS_LOG_KEYS = (
        "swapchain",
        "count",
        "present_count",
        "last_vulkan_present_id",
        "chain_has_present_id",
        "present_id_at_head",
        "want_inject",
        "injected_present_id",
        "present_id",
        "result",
        "display_latency_us",
        "latest_marker_present_id",
        "last_present_marker_present_id",
        "last_oob_present_present_id",
        "last_input_sample_present_id",
        "swapchain_latency_mode",
        "vk_nv_low_latency2_advertised",
        "vk_nv_low_latency2_requested",
        "vk_khr_present_wait_advertised",
        "vk_khr_present_wait_requested",
        "vk_khr_present_id_advertised",
        "vk_khr_present_id_requested",
    )

    # Extra fields surfaced only under --dump-latency-data. Present mode +
    # queue depth (FIFO/IMMEDIATE/MAILBOX is the vsync/VRR path, min_image_count
    # the present-queue depth) and the Reflex sleep-mode (boost / FPS-cap)
    # internals.
    _DUMP_STATUS_LOG_KEYS = (
        "present_mode",
        "present_mode_name",
        "min_image_count",
        "image_width",
        "image_height",
        "has_latency_sleep_mode",
        "low_latency_mode",
        "low_latency_boost",
        "minimum_interval_us",
        "vk_nv_low_latency2_functions",
    )

    # Reflex sleep-mode fields that, when unchanged, make a set event a duplicate.
    _SLEEP_MODE_SIG_KEYS = (
        "has_latency_sleep_mode",
        "low_latency_mode",
        "low_latency_boost",
        "minimum_interval_us",
    )

    def _sleep_mode_is_duplicate(self, sample: dict) -> bool:
        """True when a per-frame sleep-mode-set repeats the last logged state.

        Keeps the boost/cap transitions in the log without one line per frame.
        """
        if str(sample.get("event") or "") != "latency-sleep-mode-set":
            return False
        sig = tuple(sample.get(key) for key in self._SLEEP_MODE_SIG_KEYS)
        key = (sample.get("pid"), sample.get("swapchain"))
        if self._last_sleep_mode_sig.get(key) == sig:
            return True
        self._last_sleep_mode_sig[key] = sig
        return False

    def _maybe_log_status(self, sample: dict) -> None:
        event = str(sample.get("event") or "")
        events = self._STATUS_LOG_EVENTS
        keys = self._STATUS_LOG_KEYS
        if self._dump_latency_data:
            events = events + self._DUMP_STATUS_LOG_EVENTS
            keys = keys + self._DUMP_STATUS_LOG_KEYS
        if event not in events:
            return
        if self._sleep_mode_is_duplicate(sample):
            return
        fields = [
            f"event=layer-status:{event}",
            f"pid={sample.get('pid', 'unknown')}",
        ]
        for key in keys:
            if key in sample:
                fields.append(f"{key}={sample[key]}")
        self.log(f"{self.time_strftime('%Y-%m-%d %H:%M:%S')} {' '.join(fields)}")

    def _maybe_log_raw_timing(self, sample: dict) -> None:
        if sample.get("type") != "timing" or self.raw_log_interval_s is None:
            return
        now = self.time_monotonic()
        if (
            self.raw_log_interval_s > 0
            and now - self._last_raw_log_monotonic < self.raw_log_interval_s
        ):
            return
        self._last_raw_log_monotonic = now
        fields = [
            "event=latency-raw",
            f"pid={sample.get('pid', 'unknown')}",
        ]
        for key in (
            "measurement",
            "device",
            "swapchain",
            "quality",
            "present_count",
            "present_frametime_us",
            "present_id",
            "display_latency_us",
        ):
            if key in sample:
                fields.append(f"{key}={sample[key]}")
        self.log(f"{self.time_strftime('%Y-%m-%d %H:%M:%S')} {' '.join(fields)}")


def start_latency_telemetry_logger(
    *,
    log: Callable[[str], None],
    path: Path | None = None,
    dump_latency_data: bool | None = None,
) -> LatencyTelemetryLogger | None:
    try:
        return LatencyTelemetryLogger(
            path=path, log=log, dump_latency_data=dump_latency_data
        ).start()
    except Exception as exc:
        log(f"Latency telemetry unavailable: {exc}")
        return None
