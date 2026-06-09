from __future__ import annotations

from dataclasses import dataclass
import argparse
import json
import re
import statistics
import sys
from pathlib import Path
from typing import Iterable, Mapping


MARKER_ID_KEYS = (
    "latest_marker_present_id",
    "last_input_sample_present_id",
    "last_simulation_present_id",
    "last_render_submit_present_id",
    "last_present_marker_present_id",
    "last_oob_render_submit_present_id",
    "last_oob_present_present_id",
)


JOURNAL_PREFIX_RE = re.compile(r"^\w{3}\s+\d+\s+\S+\s+\S+\[\d+\]:\s+")
MS_VALUE_RE = re.compile(r"^-?\d+(?:\.\d+)?ms$")


@dataclass(frozen=True)
class FlowDiagnosis:
    root_cause: str
    summary: str
    recommendation: str
    evidence: tuple[str, ...]
    stats: Mapping[str, object]

    def format_text(self) -> str:
        lines = [
            f"root_cause={self.root_cause}",
            f"summary={self.summary}",
            f"recommendation={self.recommendation}",
        ]
        if self.evidence:
            lines.append("evidence:")
            lines.extend(f"- {item}" for item in self.evidence)
        return "\n".join(lines)


def _coerce_value(value: object) -> object:
    if not isinstance(value, str):
        return value
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    lower = value.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    try:
        if value.startswith(("0x", "0X")):
            return int(value, 16)
        return int(value)
    except ValueError:
        return value


def parse_latency_log_line(line: str) -> dict[str, object] | None:
    """Parse receiver key/value output or raw JSON layer output."""
    stripped = line.strip()
    if not stripped:
        return None

    json_start = stripped.find("{")
    if json_start >= 0:
        try:
            data = json.loads(stripped[json_start:])
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(data, dict):
                return {str(key): _coerce_value(value) for key, value in data.items()}

    sample: dict[str, object] = {}
    for token in stripped.split():
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        sample[key] = _coerce_value(value)
    return sample or None


def _status_name(sample: Mapping[str, object]) -> str | None:
    event = sample.get("event")
    if event == "latency-layer-status":
        status = sample.get("status")
        return str(status) if status is not None else None
    if sample.get("type") == "status" and event is not None:
        return str(event)
    return None


def _as_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _max_marker_id(sample: Mapping[str, object]) -> int:
    return max((_as_int(sample.get(key)) for key in MARKER_ID_KEYS), default=0)


def _is_stale_driver_report_snapshot(sample: Mapping[str, object]) -> bool:
    duplicate_count = _as_int(sample.get("driver_report_duplicate_count"))
    last_driver = _as_int(sample.get("last_driver_report_present_id"))
    if duplicate_count <= 0 or last_driver <= 0:
        return False

    return (
        _max_marker_id(sample) > last_driver
        or _as_int(sample.get("last_vulkan_present_id")) > last_driver
        or _as_int(sample.get("present_count")) > last_driver
    )


def _format_modes(modes: Iterable[object]) -> str:
    labels = sorted({str(mode) for mode in modes if mode not in (None, "", "UNKNOWN")})
    return ",".join(labels) if labels else "unknown"


def _strip_journal_prefix(line: str) -> str:
    return JOURNAL_PREFIX_RE.sub("", line.strip())


def _merge_continuation_samples(samples: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    merged: list[dict[str, object]] = []
    for sample in samples:
        if merged and "event" not in sample:
            merged[-1].update(sample)
            continue
        merged.append(sample)
    return merged


def _as_float_ms(value: object) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str):
        return 0.0
    stripped = value.strip()
    if not MS_VALUE_RE.match(stripped):
        return 0.0
    return float(stripped[:-2])


def analyze_latency_flow_lines(lines: Iterable[str]) -> FlowDiagnosis:
    samples = _merge_continuation_samples(
        sample
        for line in lines
        if (sample := parse_latency_log_line(_strip_journal_prefix(line)))
    )

    status_samples = [sample for sample in samples if _status_name(sample)]
    create_swapchains = [
        sample for sample in status_samples if _status_name(sample) == "create-swapchain"
    ]
    stale_events = [
        sample for sample in status_samples if _status_name(sample) == "latency-stream-stale"
    ]
    stale_status_snapshots = [
        sample for sample in status_samples if _is_stale_driver_report_snapshot(sample)
    ]
    present_flow_events = [
        sample for sample in status_samples if _status_name(sample) == "present-flow"
    ]
    dxvk_driver_empty = [
        sample
        for sample in status_samples
        if _status_name(sample) == "dxvk-driver-report-empty"
    ]
    dxvk_driver_misses = [
        sample
        for sample in status_samples
        if _status_name(sample) == "dxvk-driver-report-miss"
    ]
    dxvk_driver_lag_selected = [
        sample
        for sample in status_samples
        if _status_name(sample) == "dxvk-driver-report-lag-selected"
    ]
    raw_samples = [sample for sample in samples if sample.get("event") == "latency-raw"]
    meter_samples = [sample for sample in samples if sample.get("event") == "latency-meter"]
    flow_sample_count = len(status_samples) + len(raw_samples) + len(meter_samples)

    present_modes = tuple(sample.get("present_mode_name") for sample in create_swapchains)
    immediate_seen = "IMMEDIATE" in {str(mode) for mode in present_modes}
    highest_live_swapchain_count = max(
        (_as_int(sample.get("live_swapchain_count")) for sample in status_samples),
        default=0,
    )
    raw_present_ids = {
        _as_int(sample.get("present_id"))
        for sample in raw_samples
        if _as_int(sample.get("present_id")) > 0
    }
    raw_gpu_render_values = {
        _as_int(sample.get("gpu_render_us"))
        for sample in raw_samples
        if _as_int(sample.get("gpu_render_us")) > 0
    }
    raw_driver_timestamp_samples = sum(
        1
        for sample in raw_samples
        if any(
            _as_int(sample.get(key)) > 0
            for key in (
                "driver_start_us",
                "driver_end_us",
                "gpu_render_start_us",
                "gpu_render_end_us",
            )
        )
    )
    present_pacing_values = [
        (
            _as_int(sample.get("present-fps")),
            _as_float_ms(sample.get("present-frametime-p95")),
        )
        for sample in meter_samples
        if _as_int(sample.get("present-fps")) > 0
        and _as_float_ms(sample.get("present-frametime-p95")) > 0.0
    ]
    present_fps_values = [fps for fps, _frametime_ms in present_pacing_values]
    present_frametime_ms_values = [
        frametime_ms for _fps, frametime_ms in present_pacing_values
    ]
    present_pacing_samples = len(present_pacing_values)

    evidence = [
        f"parsed_lines={len(samples)} flow_lines={flow_sample_count} "
        f"status_events={len(status_samples)} "
        f"raw_samples={len(raw_samples)} meter_samples={len(meter_samples)}",
        f"create_swapchains={len(create_swapchains)} present_modes={_format_modes(present_modes)}",
        f"present_flow_events={len(present_flow_events)} stale_events={len(stale_events)} "
        f"stale_status_snapshots={len(stale_status_snapshots)}",
        f"dxvk_driver_report_empty={len(dxvk_driver_empty)} "
        f"dxvk_driver_report_lag_selected={len(dxvk_driver_lag_selected)} "
        f"dxvk_driver_report_miss={len(dxvk_driver_misses)}",
        f"highest_live_swapchain_count={highest_live_swapchain_count}",
        f"distinct_raw_present_ids={len(raw_present_ids)}",
        f"distinct_gpu_render_us={len(raw_gpu_render_values)}",
        f"raw_driver_timestamp_samples={raw_driver_timestamp_samples}",
    ]
    if present_pacing_samples:
        present_fps_median = statistics.median(present_fps_values)
        present_frametime_ms_median = statistics.median(present_frametime_ms_values)
        evidence.append(
            f"present_pacing_samples={present_pacing_samples} "
            f"present_fps_min={min(present_fps_values)} "
            f"present_fps_median={present_fps_median:g} "
            f"present_fps_max={max(present_fps_values)} "
            f"present_frametime_p95_ms_min={min(present_frametime_ms_values):.2f} "
            f"present_frametime_p95_ms_median={present_frametime_ms_median:.2f} "
            f"present_frametime_p95_ms_max={max(present_frametime_ms_values):.2f}"
        )

    stats: dict[str, object] = {
        "samples": len(samples),
        "flow_samples": flow_sample_count,
        "status_events": len(status_samples),
        "raw_samples": len(raw_samples),
        "meter_samples": len(meter_samples),
        "create_swapchains": len(create_swapchains),
        "present_modes": present_modes,
        "immediate_seen": immediate_seen,
        "present_flow_events": len(present_flow_events),
        "stale_events": len(stale_events),
        "stale_status_snapshots": len(stale_status_snapshots),
        "dxvk_driver_report_empty": len(dxvk_driver_empty),
        "dxvk_driver_report_lag_selected": len(dxvk_driver_lag_selected),
        "dxvk_driver_report_miss": len(dxvk_driver_misses),
        "highest_live_swapchain_count": highest_live_swapchain_count,
        "distinct_raw_present_ids": len(raw_present_ids),
        "distinct_gpu_render_us": len(raw_gpu_render_values),
        "raw_driver_timestamp_samples": raw_driver_timestamp_samples,
        "present_pacing_samples": present_pacing_samples,
    }
    if present_pacing_samples:
        stats["present_fps_min"] = min(present_fps_values)
        stats["present_fps_median"] = statistics.median(present_fps_values)
        stats["present_fps_max"] = max(present_fps_values)
        stats["present_frametime_p95_ms_min"] = min(present_frametime_ms_values)
        stats["present_frametime_p95_ms_median"] = statistics.median(
            present_frametime_ms_values
        )
        stats["present_frametime_p95_ms_max"] = max(present_frametime_ms_values)

    if dxvk_driver_lag_selected and dxvk_driver_misses and raw_driver_timestamp_samples == 0:
        latest_lag = dxvk_driver_lag_selected[-1]
        latest_miss = dxvk_driver_misses[-1]
        selected = _as_int(latest_lag.get("selected_driver_report_present_id"))
        requested = _as_int(latest_lag.get("requested_present_id"))
        newest = _as_int(latest_lag.get("newest_driver_report_present_id"))
        lag_frames = _as_int(latest_lag.get("driver_report_lag_frames"))
        last_driver = _as_int(latest_miss.get("last_driver_report_present_id"))
        latest_count = _as_int(latest_miss.get("count"))
        evidence.extend(
            (
                f"latest_dxvk_lag.selected_driver_report_present_id={selected}",
                f"latest_dxvk_lag.requested_present_id={requested}",
                f"latest_dxvk_lag.newest_driver_report_present_id={newest}",
                f"latest_dxvk_lag.driver_report_lag_frames={lag_frames}",
                f"latest_dxvk_miss.count={latest_count}",
                f"latest_dxvk_miss.last_driver_report_present_id={last_driver}",
            )
        )
        stats["latest_dxvk_driver_report_lag_selected"] = dict(latest_lag)
        stats["latest_dxvk_driver_report_miss"] = dict(latest_miss)

        recommendation = (
            (
                "Do not use the Reflex driver-report path as RE9 GPU render "
                "latency on this stack. Use present-frametime-p95 / present-fps "
                "as a pre-frame-generation base-frame cadence signal, and label "
                "it separately from GPU render latency."
            )
            if present_pacing_samples
            else (
                "Do not use the Reflex driver-report path as RE9 GPU render "
                "latency on this stack. Repeat with latency-meter lines captured "
                "to verify the present-frametime-p95 / present-fps base-frame "
                "cadence fallback."
            )
        )
        return FlowDiagnosis(
            root_cause="dxvk-nvapi-driver-report-lagged-then-stalled",
            summary=(
                "DXVK-NVAPI could find lagged NVIDIA Reflex timing reports for "
                "a while, then only misses remained. The driver report IDs "
                "stopped advancing and the reports never carried usable GPU "
                "render timestamps."
            ),
            recommendation=recommendation,
            evidence=tuple(evidence),
            stats=stats,
        )

    stale_snapshots = stale_events or stale_status_snapshots
    if stale_snapshots:
        stale = stale_snapshots[-1]
        stale_status = _status_name(stale) or "unknown"
        stale_source = "latency-stream-stale" if stale_events else stale_status
        live_count = _as_int(stale.get("live_swapchain_count"))
        last_driver = _as_int(stale.get("last_driver_report_present_id"))
        last_vulkan = _as_int(stale.get("last_vulkan_present_id"))
        marker_id = _max_marker_id(stale)
        duplicate_count = _as_int(stale.get("driver_report_duplicate_count"))
        latency_mode = stale.get("swapchain_latency_mode")

        evidence.extend(
            (
                f"latest_stale.source_status={stale_source}",
                f"latest_stale.live_swapchain_count={live_count}",
                f"latest_stale.last_driver_report_present_id={last_driver}",
                f"latest_stale.last_vulkan_present_id={last_vulkan}",
                f"latest_stale.max_marker_present_id={marker_id}",
                f"latest_stale.driver_report_duplicate_count={duplicate_count}",
                f"latest_stale.swapchain_latency_mode={latency_mode}",
            )
        )
        stats["latest_stale"] = dict(stale)

        if live_count > 1:
            return FlowDiagnosis(
                root_cause="vkd3d-multi-swapchain-reflex-guard",
                summary=(
                    "Reflex timings stalled while more than one Vulkan swapchain "
                    "was live. That matches VKD3D-Proton's path that clears the "
                    "device low-latency swapchain when vk_swapchain_count > 1."
                ),
                recommendation=(
                    "Try avoiding the second swapchain if a game setting causes it; "
                    "otherwise the likely workaround is a VKD3D-Proton patch/test "
                    "build that keeps or restores low_latency_swapchain for transient "
                    "recreation."
                ),
                evidence=tuple(evidence),
                stats=stats,
            )

        if latency_mode is False:
            return FlowDiagnosis(
                root_cause="swapchain-missing-vk-nv-low-latency2-create-info",
                summary=(
                    "The stream stalled on a swapchain where latencyModeEnable was "
                    "not visible at vkCreateSwapchainKHR."
                ),
                recommendation=(
                    "Fix layer ordering or the DXVK-NVAPI/VKD3D create-info path so "
                    "VkSwapchainLatencyCreateInfoNV(latencyModeEnable=true) reaches "
                    "the driver."
                ),
                evidence=tuple(evidence),
                stats=stats,
            )

        if last_driver and marker_id > last_driver and last_vulkan > last_driver:
            return FlowDiagnosis(
                root_cause="nvidia-reflex-timing-ring-stale",
                summary=(
                    "The app/VKD3D side kept advancing Reflex markers and Vulkan "
                    "present IDs beyond the last driver timing report, but "
                    "vkGetLatencyTimingsNV kept returning the same report."
                ),
                recommendation=(
                    "Do not display the frozen Reflex value. Keep the stale guard, "
                    "and use a no-build workaround only if changing present mode "
                    "prevents this condition in a live run."
                ),
                evidence=tuple(evidence),
                stats=stats,
            )

        if last_driver and marker_id > last_driver:
            return FlowDiagnosis(
                root_cause="driver-report-stale-after-reflex-markers",
                summary=(
                    "Reflex markers advanced past the last driver timing report, but "
                    "Vulkan present IDs did not prove the present side advanced."
                ),
                recommendation=(
                    "Capture with PENGUIN_BURNER_LATENCY_DEBUG_FLOW=1 and check "
                    "present-flow / last_vulkan_present_id around the stale point."
                ),
                evidence=tuple(evidence),
                stats=stats,
            )

        return FlowDiagnosis(
            root_cause="stale-driver-report-insufficient-flow-evidence",
            summary=(
                "The driver report stream stalled, but the captured fields are not "
                "enough to decide whether VKD3D ownership, marker feed, or the "
                "driver timing ring caused it."
            ),
            recommendation=(
                "Repeat with PENGUIN_BURNER_LATENCY_DEBUG_FLOW=1 and verify "
                "create-swapchain, present-flow, and latency-stream-stale lines."
            ),
            evidence=tuple(evidence),
            stats=stats,
        )

    if (
        present_pacing_samples
        and raw_driver_timestamp_samples == 0
        and not raw_gpu_render_values
    ):
        return FlowDiagnosis(
            root_cause="present-cadence-only-no-driver-gpu-timestamps",
            summary=(
                "The capture has live Vulkan present pacing, but no usable driver "
                "GPU render timestamps. This is a base-frame pacing signal, not "
                "the NVIDIA Reflex GPU render-latency signal."
            ),
            recommendation=(
                "For RE9 with frame generation enabled, use present-frametime-p95 "
                "/ present-fps as the pre-frame-generation base-frame cadence. "
                "Do not label it GPU render latency, and multiply present-fps by "
                "the frame-generation factor only as an output-FPS sanity check."
            ),
            evidence=tuple(evidence),
            stats=stats,
        )

    if immediate_seen and len(raw_present_ids) >= 10 and len(raw_gpu_render_values) >= 3:
        return FlowDiagnosis(
            root_cause="no-stall-detected-with-immediate-present-mode",
            summary=(
                "No stale Reflex stream was detected in the captured window, and "
                "raw driver reports advanced while VKD3D used IMMEDIATE present mode."
            ),
            recommendation=(
                "Treat VKD3D_SWAPCHAIN_PRESENT_MODE=IMMEDIATE as the current RE9 "
                "workaround only after the capture spans the menu-to-gameplay "
                "transition that previously stalled."
            ),
            evidence=tuple(evidence),
            stats=stats,
        )

    if flow_sample_count == 0:
        return FlowDiagnosis(
            root_cause="no-latency-flow-events",
            summary="No PenguinBurner latency flow events were found in the input.",
            recommendation=(
                "Start the game with the PenguinBurner latency layer enabled, then "
                "pipe the filtered journal output into this analyzer."
            ),
            evidence=tuple(evidence),
            stats=stats,
        )

    return FlowDiagnosis(
        root_cause="inconclusive-no-stale-event",
        summary=(
            "The input contains latency events but no stale-flow snapshot and not "
            "enough advancing raw Reflex reports to call the workaround proven."
        ),
        recommendation=(
            "Capture through the RE9 menu-to-gameplay transition, then rerun the "
            "analyzer on the filtered journal output."
        ),
        evidence=tuple(evidence),
        stats=stats,
    )


def _read_lines(path: str | None) -> list[str]:
    if not path or path == "-":
        return list(sys.stdin)
    return Path(path).read_text(encoding="utf-8", errors="replace").splitlines()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Classify PenguinBurner Reflex latency flow logs."
    )
    parser.add_argument(
        "path",
        nargs="?",
        default="-",
        help="Log file to analyze, or '-' / omitted for stdin.",
    )
    args = parser.parse_args(argv)

    diagnosis = analyze_latency_flow_lines(_read_lines(args.path))
    print(diagnosis.format_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
