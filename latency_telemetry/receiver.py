from __future__ import annotations

from collections import deque
import json
import os
import pwd
from pathlib import Path
import select
import socket
import threading
import time
from typing import Callable

from .layer_check import DEFAULT_LATENCY_LAYER_LAUNCH_OPTIONS

MARKER_INPUT_SAMPLE_BIT = 1 << 6
RAW_TIMING_LOG_ENV = "PENGUIN_BURNER_LATENCY_RAW_TIMING_LOG"

QUALITY_RANK = {
    "none": 0,
    "present-frametime": 1,
    "driver-timing": 2,
    "reflex-marker-presence": 2,
    "reflex-marker-simulation": 2,
    "reflex-markers": 2,
    "reflex-marker-render-submit": 3,
    "reflex-marker-input-present": 3,
    "reflex-render-submit": 3,
    "reflex-simulation": 3,
    "reflex-input-present": 5,
}

METER_SAMPLE_MAX_AGE_S = 5.0
STALE_DRIVER_REPORT_MAX_AGE_S = 30.0


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


def _format_ms(value_us: int | None) -> str:
    if value_us is None:
        return "n/a"
    return f"{value_us / 1000.0:.2f}ms"


def _int_value(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _raw_timing_log_interval(env: dict[str, str] | None = None) -> float | None:
    env = os.environ if env is None else env
    value = str(env.get(RAW_TIMING_LOG_ENV) or "").strip()
    if not value:
        return 1.0
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


def _quality_for_sample(sample: dict) -> str:
    is_marker_proxy = str(sample.get("measurement") or "") == "marker-proxy"
    if is_marker_proxy:
        if _positive_us(sample, "input_to_present_us"):
            return "reflex-marker-input-present"
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


def _latency_proxy_p95(samples: list[dict]) -> int | None:
    real_input_present = _p95_us(
        [
            _int_value(sample.get("input_to_present_us"))
            for sample in samples
            if str(sample.get("measurement") or "") != "marker-proxy"
        ]
    )
    if real_input_present is not None:
        return real_input_present

    marker_input_present = _p95_us(
        [
            _int_value(sample.get("input_to_present_us"))
            for sample in samples
            if str(sample.get("measurement") or "") == "marker-proxy"
        ]
    )
    if marker_input_present is not None:
        return marker_input_present

    return _p95_us([_int_value(sample.get("render_submit_us")) for sample in samples])


class LatencyTelemetryMeter:
    def __init__(
        self,
        *,
        max_samples: int = 240,
        max_sample_age_s: float = METER_SAMPLE_MAX_AGE_S,
        stale_driver_report_max_age_s: float = STALE_DRIVER_REPORT_MAX_AGE_S,
        time_monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._samples = deque(maxlen=max_samples)
        self._max_sample_age_s = float(max_sample_age_s)
        self._stale_driver_report_max_age_s = float(stale_driver_report_max_age_s)
        self._time_monotonic = time_monotonic
        self._last_ignored_driver_report: dict | None = None

    def add_sample(self, sample: dict) -> None:
        if sample.get("type") != "timing":
            return
        stored = dict(sample)
        stored["_received_monotonic"] = self._time_monotonic()
        if (
            str(sample.get("measurement") or "") == "driver-report"
            and _int_value(sample.get("driver_report_duplicate_count")) > 0
        ):
            self._last_ignored_driver_report = stored
            return
        if str(sample.get("measurement") or "") == "driver-report":
            self._last_ignored_driver_report = None
        self._samples.append(stored)

    def summary(self, *, now: float | None = None) -> str | None:
        if not self._samples:
            return self._stale_driver_report_summary(now=now)
        now = self._time_monotonic() if now is None else now
        samples = [
            sample
            for sample in self._samples
            if now - float(sample.get("_received_monotonic") or 0.0)
            <= self._max_sample_age_s
        ]
        if not samples:
            return self._stale_driver_report_summary(now=now)
        latest = samples[-1]
        best_quality = max(
            (_quality_for_sample(sample) for sample in samples),
            key=lambda quality: QUALITY_RANK.get(quality, 0),
        )
        gpu_frame_p95 = _p95_us(
            [_int_value(sample.get("gpu_frame_time_us")) for sample in samples]
        )
        input_present_p95 = _p95_us(
            [_int_value(sample.get("input_to_present_us")) for sample in samples]
        )
        render_submit_p95 = _p95_us(
            [_int_value(sample.get("render_submit_us")) for sample in samples]
        )
        render_present_p95 = _p95_us(
            [_int_value(sample.get("render_present_us")) for sample in samples]
        )
        gpu_render_p95 = _p95_us(
            [_int_value(sample.get("gpu_render_us")) for sample in samples]
        )
        latency_proxy_p95 = _latency_proxy_p95(samples)
        missing_hints = _missing_metric_hints(
            samples,
            input_present_p95=input_present_p95,
            gpu_frame_p95=gpu_frame_p95,
        )
        missing_text = (
            f" missing={','.join(missing_hints)}" if missing_hints else ""
        )
        return (
            f"event=latency-meter pid={latest.get('pid', 'unknown')} "
            f"quality={best_quality} samples={len(samples)} "
            f"latency-proxy-p95={_format_ms(latency_proxy_p95)} "
            f"render-submit-p95={_format_ms(render_submit_p95)} "
            f"render-present-p95={_format_ms(render_present_p95)} "
            f"gpu-render-p95={_format_ms(gpu_render_p95)} "
            f"input-present-p95={_format_ms(input_present_p95)} "
            f"gpu-frame-p95={_format_ms(gpu_frame_p95)}"
            f"{missing_text}"
        )

    def _stale_driver_report_summary(self, *, now: float | None = None) -> str | None:
        latest = self._last_ignored_driver_report
        if latest is None:
            return None
        now = self._time_monotonic() if now is None else now
        age_s = now - float(latest.get("_received_monotonic") or 0.0)
        if age_s > self._stale_driver_report_max_age_s:
            return None
        return (
            f"event=latency-meter pid={latest.get('pid', 'unknown')} "
            "quality=stale-driver-report samples=0 "
            "latency-proxy-p95=n/a render-submit-p95=n/a "
            "render-present-p95=n/a gpu-render-p95=n/a "
            "input-present-p95=n/a gpu-frame-p95=n/a "
            f"stale-present_id={latest.get('present_id', 'unknown')} "
            "stale-driver-report-duplicates="
            f"{_int_value(latest.get('driver_report_duplicate_count'))} "
            f"stale-age-s={age_s:.1f} missing=fresh-samples"
        )


class LatencyTelemetryLogger:
    def __init__(
        self,
        *,
        path: Path | None = None,
        paths: list[Path] | None = None,
        log: Callable[[str], None],
        log_interval_s: float = 10.0,
        raw_log_interval_s: float | None = None,
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
        self.time_monotonic = time_monotonic
        self.time_strftime = time_strftime
        self.meter = LatencyTelemetryMeter(time_monotonic=time_monotonic)
        self._socket: socket.socket | None = None
        self._sockets: list[tuple[socket.socket, Path]] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_log_monotonic = 0.0
        self._last_raw_log_monotonic = 0.0

    def start(self) -> "LatencyTelemetryLogger":
        for path in self.paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                path.unlink()
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            sock.setblocking(False)
            sock.bind(str(path))
            try:
                path.chmod(0o666)
            except OSError:
                pass
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
            if sample.get("type") == "status":
                self._log_status(sample)
            else:
                self.meter.add_sample(sample)
                self._maybe_log_raw_timing(sample)

    def _maybe_log_summary(self) -> None:
        now = self.time_monotonic()
        if now - self._last_log_monotonic < self.log_interval_s:
            return
        summary = self.meter.summary(now=now)
        if not summary:
            return
        self._last_log_monotonic = now
        self.log(f"{self.time_strftime('%Y-%m-%d %H:%M:%S')} {summary}")

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
            "source",
            "measurement",
            "device",
            "queue",
            "queue_family",
            "swapchain",
            "present_id",
            "submit_sequence",
            "quality",
            "sample_count",
            "timing_count",
            "driver_report_count",
            "driver_report_duplicate_count",
            "marker_bits",
            "render_submit_us",
            "render_present_us",
            "input_to_present_us",
            "gpu_frame_time_us",
            "gpu_render_us",
            "input_sample_us",
            "sim_start_us",
            "sim_end_us",
            "render_submit_start_us",
            "render_submit_end_us",
            "present_start_us",
            "present_end_us",
            "driver_start_us",
            "driver_end_us",
            "os_render_queue_start_us",
            "os_render_queue_end_us",
            "gpu_render_start_us",
            "gpu_render_end_us",
        ):
            if key in sample:
                fields.append(f"{key}={sample[key]}")
        self.log(f"{self.time_strftime('%Y-%m-%d %H:%M:%S')} {' '.join(fields)}")

    def _log_status(self, sample: dict) -> None:
        fields = [
            "event=latency-layer-status",
            f"status={sample.get('event', 'unknown')}",
            f"pid={sample.get('pid', 'unknown')}",
        ]
        for key in (
            "count",
            "device",
            "swapchain",
            "queue",
            "queue_family",
            "queue_flags",
            "timestamp_valid_bits",
            "simulation_start",
            "simulation_end",
            "render_submit_start",
            "render_submit_end",
            "present_start",
            "present_end",
            "input_sample",
            "out_of_band_render_submit_start",
            "out_of_band_render_submit_end",
            "out_of_band_present_start",
            "out_of_band_present_end",
            "vk_nv_low_latency2_advertised",
            "vk_nv_low_latency2_requested",
            "vk_nv_low_latency2_functions",
            "marker_count",
        ):
            if key in sample:
                fields.append(f"{key}={sample[key]}")
        self.log(f"{self.time_strftime('%Y-%m-%d %H:%M:%S')} {' '.join(fields)}")


def start_latency_telemetry_logger(
    *,
    log: Callable[[str], None],
    path: Path | None = None,
) -> LatencyTelemetryLogger | None:
    try:
        return LatencyTelemetryLogger(path=path, log=log).start()
    except Exception as exc:
        log(f"Latency telemetry unavailable: {exc}")
        return None
