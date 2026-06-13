from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Callable


@dataclass(frozen=True, slots=True)
class _CpuSample:
    pid: int
    process_ticks: int
    total_ticks: int
    thread_ticks: dict[int, int]


@dataclass(frozen=True, slots=True)
class ProcessCpuUsage:
    process_util_pct: int | None = None
    peak_thread_util_pct: int | None = None
    peak_thread_id: int | None = None
    peak_thread_name: str = ""


class ProcessCpuUsageSampler:
    """Sample process CPU pressure from /proc.

    process_util_pct is normalized to total system CPU capacity, so it stays
    in the 0-100% range. peak_thread_util_pct is normalized to one logical CPU,
    so it catches main-thread style bottlenecks.
    """

    def __init__(
        self,
        *,
        proc_root: str | Path = "/proc",
        cpu_count: int | None = None,
        read_text: Callable[[Path], str] | None = None,
    ) -> None:
        self.proc_root = Path(proc_root)
        self.cpu_count = int(cpu_count or os.cpu_count() or 1)
        self._read_text = read_text or _read_text
        self._last_sample: _CpuSample | None = None

    def sample_usage(self, pid: object) -> ProcessCpuUsage:
        current = self._read_sample(pid)
        if current is None:
            self._last_sample = None
            return ProcessCpuUsage()

        previous = self._last_sample
        self._last_sample = current
        if previous is None or previous.pid != current.pid:
            return ProcessCpuUsage()

        process_delta = current.process_ticks - previous.process_ticks
        total_delta = current.total_ticks - previous.total_ticks
        if process_delta < 0 or total_delta <= 0:
            return ProcessCpuUsage()

        peak_thread_id = None
        peak_thread_delta = -1
        for tid, thread_ticks in current.thread_ticks.items():
            previous_thread_ticks = previous.thread_ticks.get(tid)
            if previous_thread_ticks is None:
                continue
            thread_delta = thread_ticks - previous_thread_ticks
            if thread_delta >= 0 and thread_delta > peak_thread_delta:
                peak_thread_id = tid
                peak_thread_delta = thread_delta

        return ProcessCpuUsage(
            process_util_pct=_system_cpu_pct(process_delta, total_delta),
            peak_thread_util_pct=(
                _single_cpu_pct(peak_thread_delta, total_delta, self.cpu_count)
                if peak_thread_id is not None
                else None
            ),
            peak_thread_id=peak_thread_id,
        )

    def sample(self, pid: object) -> int | None:
        return self.sample_usage(pid).process_util_pct

    def _read_sample(self, pid: object) -> _CpuSample | None:
        try:
            pid_value = int(str(pid).strip())
        except (TypeError, ValueError):
            return None
        if pid_value <= 0:
            return None

        try:
            process_ticks = _process_cpu_ticks(
                self._read_text(self.proc_root / str(pid_value) / "stat")
            )
            total_ticks = _total_cpu_ticks(self._read_text(self.proc_root / "stat"))
        except (OSError, ValueError):
            return None
        return _CpuSample(
            pid=pid_value,
            process_ticks=process_ticks,
            total_ticks=total_ticks,
            thread_ticks=_thread_cpu_ticks(
                self.proc_root / str(pid_value) / "task",
                self._read_text,
            ),
        )


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _process_cpu_ticks(stat_text: str) -> int:
    return _stat_cpu_ticks(stat_text)


def _stat_cpu_ticks(stat_text: str) -> int:
    text = str(stat_text or "").strip()
    rparen = text.rfind(")")
    if rparen < 0:
        raise ValueError("missing process stat command terminator")
    fields = text[rparen + 2 :].split()
    if len(fields) <= 12:
        raise ValueError("process stat is missing CPU fields")
    return int(fields[11]) + int(fields[12])


def _thread_cpu_ticks(
    task_dir: Path,
    read_text: Callable[[Path], str],
) -> dict[int, int]:
    threads: dict[int, int] = {}
    try:
        entries = list(task_dir.iterdir())
    except OSError:
        return threads
    for entry in entries:
        try:
            tid = int(entry.name)
        except ValueError:
            continue
        try:
            threads[tid] = _stat_cpu_ticks(read_text(entry / "stat"))
        except (OSError, ValueError):
            continue
    return threads


def _total_cpu_ticks(stat_text: str) -> int:
    for line in str(stat_text or "").splitlines():
        fields = line.split()
        if fields and fields[0] == "cpu":
            return sum(int(value) for value in fields[1:])
    raise ValueError("missing aggregate CPU stat line")


def _system_cpu_pct(delta: int, total_delta: int) -> int:
    cpu_pct = (float(delta) / float(total_delta)) * 100.0
    return min(100, max(0, int(round(cpu_pct))))


def _single_cpu_pct(delta: int, total_delta: int, cpu_count: int) -> int:
    cpu_pct = (float(delta) / float(total_delta)) * float(cpu_count) * 100.0
    return min(100, max(0, int(round(cpu_pct))))
