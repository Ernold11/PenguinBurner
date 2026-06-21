from __future__ import annotations

from .models import Q2RTXStabilityResult, TelemetrySample


_QUIET_OUTPUT_TAIL_PREFIXES: tuple[str, ...] = ()
_QUIET_OUTPUT_TAIL_LINES = frozenset(
    {
        "Closing console log.",
    }
)


def _filter_report_output_tail(lines: list[str]) -> list[str]:
    filtered: list[str] = []
    for line in lines:
        stripped = str(line).strip()
        if stripped in _QUIET_OUTPUT_TAIL_LINES:
            continue
        if any(stripped.startswith(prefix) for prefix in _QUIET_OUTPUT_TAIL_PREFIXES):
            continue
        filtered.append(line)
    return filtered


def _telemetry_summary_for_samples(
    samples: list[TelemetrySample],
) -> dict[str, float | int]:
    summary: dict[str, float | int] = {"sample_count": len(samples)}
    if not samples:
        return summary

    def _values(attr: str) -> list[float]:
        values: list[float] = []
        for sample in samples:
            value = getattr(sample, attr)
            if value is not None:
                values.append(float(value))
        return values

    for attr, prefix in (
        ("gpu_util_pct", "gpu_util"),
        ("power_w", "power"),
        ("core_clock_mhz", "core_clock"),
        ("temperature_c", "temperature"),
        ("voltage_mv", "voltage"),
        ("fan_speed_pct", "fan"),
    ):
        values = _values(attr)
        if values:
            summary[f"{prefix}_avg"] = sum(values) / len(values)
            summary[f"{prefix}_max"] = max(values)
    return summary


def _telemetry_summary_text(summary: dict[str, float | int]) -> str:
    parts = [f"samples={summary['sample_count']}"]
    if "gpu_util_avg" in summary:
        parts.append(
            "gpu_util="
            f"{summary['gpu_util_avg']:.1f}% avg/{summary['gpu_util_max']:.1f}% max"
        )
    if "power_avg" in summary:
        parts.append(
            f"power={summary['power_avg']:.1f}W avg/{summary['power_max']:.1f}W max"
        )
    if "core_clock_avg" in summary:
        parts.append(
            "core_clock="
            f"{summary['core_clock_avg']:.0f}MHz avg/"
            f"{summary['core_clock_max']:.0f}MHz max"
        )
    if "voltage_avg" in summary:
        parts.append(
            "voltage="
            f"{summary['voltage_avg']:.0f}mV avg/"
            f"{summary['voltage_max']:.0f}mV max"
        )
    if "fan_avg" in summary:
        parts.append(
            f"fan={summary['fan_avg']:.0f}% avg/{summary['fan_max']:.0f}% max"
        )
    if "temperature_max" in summary:
        parts.append(f"temp_max={summary['temperature_max']:.0f}C")
    return " | ".join(parts)


def print_q2rtx_stability_result(result: Q2RTXStabilityResult) -> None:
    status = "PASS" if result.success else "FAIL"
    print(f"Stability test: {status}", flush=True)
    print(
        f"Reason: {result.reason} | workload={result.workload_name} ({result.workload_kind}) | "
        f"requested={result.duration_requested_s}s | observed={result.duration_observed_s:.1f}s",
        flush=True,
    )
    print(
        f"Executable: {result.executable_path} | workdir={result.workdir}",
        flush=True,
    )
    print(f"Log: {result.log_path}", flush=True)
    if result.demo_path is not None:
        print(f"Demo file: {result.demo_path}", flush=True)
    elif result.workload_kind == "benchmark":
        print(f"Demo asset: {result.workload_name} (found in game data)", flush=True)
    print(
        f"Shutdown: {result.shutdown_mode} | exit_code={result.process_exit_code}",
        flush=True,
    )

    if result.benchmark_summary is not None:
        benchmark = result.benchmark_summary
        fps_min = (
            f"{float(benchmark.fps_min):.1f}"
            if benchmark.fps_min is not None
            else "n/a"
        )
        fps_max = (
            f"{float(benchmark.fps_max):.1f}"
            if benchmark.fps_max is not None
            else "n/a"
        )
        fps_mean = (
            f"{float(benchmark.fps_mean):.1f}"
            if benchmark.fps_mean is not None
            else "n/a"
        )
        print(
            "Benchmark: "
            f"reason={benchmark.reason or 'done'} | "
            f"loops={int(benchmark.loops)} | "
            f"render_frames={int(benchmark.render_frames)} | "
            f"demo_frames={benchmark.demo_frames if benchmark.demo_frames is not None else 'n/a'} | "
            f"measured={float(benchmark.measured_s):.3f}s | "
            f"fps={fps_min}/{float(benchmark.fps_avg):.1f}/{fps_max}/{fps_mean} "
            "(min/avg/max/mean)",
            flush=True,
        )
    else:
        print("Workload metrics: none", flush=True)

    summary = result.telemetry_summary()
    if summary.get("sample_count", 0):
        print("Telemetry: " + _telemetry_summary_text(summary), flush=True)
    else:
        print("Telemetry: unavailable", flush=True)
    companion_summary = _telemetry_summary_for_samples(
        list(result.companion_telemetry_samples or [])
    )
    if companion_summary.get("sample_count", 0):
        print(
            "CUDA companion telemetry: "
            + _telemetry_summary_text(companion_summary),
            flush=True,
        )

    if result.fatal_output_matches:
        print(
            "Fatal output matches: " + ", ".join(result.fatal_output_matches),
            flush=True,
        )
    if result.xid_messages:
        print("Xid messages:", flush=True)
        for line in result.xid_messages:
            print(f"  {line}", flush=True)
    output_tail = _filter_report_output_tail(result.output_tail)
    if output_tail:
        print("Output tail:", flush=True)
        for line in output_tail[-10:]:
            print(f"  {line}", flush=True)
