from __future__ import annotations

from datetime import datetime, timedelta
import shutil
import subprocess

from hidden_nvapi_voltage import create_hidden_voltage_reader
from nvml_perf_cap_reason import NvmlPerfCapReasonReader
from subprocess_locale import stable_subprocess_env

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


class _HiddenNvmlVoltageSession:
    def __init__(self, gpu_index: int):
        self._gpu_index = int(gpu_index)
        self._voltage_reader = None
        self._perf_cap_reader = None
        try:
            self._voltage_reader = create_hidden_voltage_reader(
                gpu_index=self._gpu_index
            )
        except Exception:
            self._voltage_reader = None
        try:
            self._perf_cap_reader = NvmlPerfCapReasonReader(gpu_index=self._gpu_index)
        except Exception:
            self._perf_cap_reader = None

    def read_live_voltage_mv(self) -> float | None:
        if self._voltage_reader is None:
            return None
        try:
            voltage_uv = self._voltage_reader.read_microvolts()
        except Exception:
            return None
        if voltage_uv is None:
            return None
        return float(int(voltage_uv) / 1000.0)

    def read_perf_cap_reason(self) -> str | None:
        if self._perf_cap_reader is None:
            return None
        try:
            return self._perf_cap_reader.read_reason()
        except Exception:
            return None

    def close(self) -> None:
        if self._voltage_reader is not None:
            try:
                self._voltage_reader.close()
            except Exception:
                pass
        self._voltage_reader = None
        if self._perf_cap_reader is not None:
            try:
                self._perf_cap_reader.close()
            except Exception:
                pass
        self._perf_cap_reader = None


def _parse_metric(value: str) -> float | None:
    cleaned = value.strip()
    if not cleaned or cleaned.upper() in {"N/A", "[N/A]", "[NOT SUPPORTED]"}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def query_gpu_metrics(
    gpu_index: int,
    *,
    voltage_session: _HiddenNvmlVoltageSession | None = None,
) -> TelemetrySample | None:
    nvidia_smi = shutil.which("nvidia-smi")
    voltage_mv = (
        voltage_session.read_live_voltage_mv() if voltage_session is not None else None
    )
    perf_cap_reason = (
        voltage_session.read_perf_cap_reason() if voltage_session is not None else None
    )
    if not nvidia_smi:
        if voltage_mv is None and perf_cap_reason is None:
            return None
        return TelemetrySample(
            elapsed_s=0.0,
            gpu_util_pct=None,
            power_w=None,
            core_clock_mhz=None,
            temperature_c=None,
            voltage_mv=voltage_mv,
            fan_speed_pct=None,
            perf_cap_reason=perf_cap_reason,
        )

    command = [
        nvidia_smi,
        "--query-gpu=utilization.gpu,power.draw,clocks.gr,temperature.gpu,fan.speed",
        "--format=csv,noheader,nounits",
        "-i",
        str(int(gpu_index)),
    ]
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
        if voltage_mv is None and perf_cap_reason is None:
            return None
        return TelemetrySample(
            elapsed_s=0.0,
            gpu_util_pct=None,
            power_w=None,
            core_clock_mhz=None,
            temperature_c=None,
            voltage_mv=voltage_mv,
            fan_speed_pct=None,
            perf_cap_reason=perf_cap_reason,
        )

    line = result.stdout.strip().splitlines()
    if not line:
        return None
    fields = [item.strip() for item in line[0].split(",")]
    while len(fields) < 5:
        fields.append("")
    return TelemetrySample(
        elapsed_s=0.0,
        gpu_util_pct=_parse_metric(fields[0]),
        power_w=_parse_metric(fields[1]),
        core_clock_mhz=_parse_metric(fields[2]),
        temperature_c=_parse_metric(fields[3]),
        voltage_mv=voltage_mv,
        fan_speed_pct=_parse_metric(fields[4]),
        perf_cap_reason=perf_cap_reason,
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
