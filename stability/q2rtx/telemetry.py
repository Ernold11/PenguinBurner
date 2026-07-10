from __future__ import annotations

from datetime import datetime, timedelta
import shutil
import subprocess

from common.subprocess_locale import stable_subprocess_env
from drivers.nvidia.daemon_gpu import (
    DaemonGpuClient,
    format_perf_cap_reason_mask,
)

from .models import TelemetrySample


def _xid_message_is_at_or_after(line: str, started_at: datetime) -> bool:
    parts = str(line).strip().split(maxsplit=2)
    if not parts:
        return True

    timestamp_text = str(parts[0])
    if "T" not in timestamp_text and len(parts) >= 2:
        timestamp_text = f"{parts[0]}T{parts[1]}"
    try:
        message_time = datetime.fromisoformat(timestamp_text).astimezone()
    except ValueError:
        return True

    cutoff = started_at.astimezone() - timedelta(seconds=1)
    return message_time >= cutoff


def query_gpu_metrics(
    gpu_index: int,
    *,
    gpu_client: DaemonGpuClient | None = None,
) -> TelemetrySample | None:
    client = gpu_client or DaemonGpuClient(int(gpu_index))
    try:
        telemetry = client.telemetry(refresh=True)
    except Exception:
        return None
    voltage_mv = (
        telemetry.voltage_mv
        if telemetry.voltage_uv is None or 300_000 <= telemetry.voltage_uv <= 1_500_000
        else None
    )
    return TelemetrySample(
        elapsed_s=0.0,
        gpu_util_pct=telemetry.utilization_pct,
        power_w=telemetry.power_draw_w,
        core_clock_mhz=telemetry.clocks.graphics_mhz,
        temperature_c=telemetry.temperature_c,
        voltage_mv=voltage_mv,
        fan_speed_pct=(
            telemetry.fan_speeds_pct[0] if telemetry.fan_speeds_pct else None
        ),
        perf_cap_reason=(
            format_perf_cap_reason_mask(telemetry.throttle_reason_mask)
            if telemetry.throttle_reason_mask is not None
            else None
        ),
    )


def _query_xid_messages_since(started_at: datetime) -> list[str]:
    since_value = started_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    commands: list[list[str]] = []
    journalctl = shutil.which("journalctl")
    if journalctl:
        commands.append(
            [
                journalctl,
                "-k",
                "--since",
                since_value,
                "--no-pager",
                "--output=short-iso",
            ]
        )
    dmesg = shutil.which("dmesg")
    if dmesg:
        commands.append([dmesg, "--since", since_value])

    for command in commands:
        try:
            result = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=stable_subprocess_env(),
            )
        except (OSError, subprocess.SubprocessError):
            continue

        messages = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if "NVRM: Xid" not in line:
                continue
            if not _xid_message_is_at_or_after(line, started_at):
                continue
            messages.append(line)
        if messages:
            return messages

    return []
