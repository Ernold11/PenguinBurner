from __future__ import annotations

from pathlib import Path

from runtime.gpu_control.process_cpu_sampler import ProcessCpuUsageSampler


def test_process_cpu_usage_sampler_returns_none_until_second_sample() -> None:
    reads = {
        "/proc/123/stat": [
            "123 (Game Thread) S 1 1 1 0 0 0 0 0 0 0 20 10 0 0 0",
            "123 (Game Thread) S 1 1 1 0 0 0 0 0 0 0 50 20 0 0 0",
        ],
        "/proc/stat": [
            "cpu  100 0 100 1800 0 0 0 0 0 0\n",
            "cpu  120 0 130 1950 0 0 0 0 0 0\n",
        ],
    }

    def read_text(path: Path) -> str:
        try:
            return reads[str(path)].pop(0)
        except KeyError as exc:
            raise OSError(str(path)) from exc

    sampler = ProcessCpuUsageSampler(cpu_count=4, read_text=read_text)

    assert sampler.sample(123) is None
    assert sampler.sample(123) == 20


def test_process_cpu_usage_sampler_resets_when_pid_changes() -> None:
    reads = {
        "/proc/123/stat": ["123 (first) S 1 1 1 0 0 0 0 0 0 0 20 10 0 0 0"],
        "/proc/456/stat": ["456 (second) S 1 1 1 0 0 0 0 0 0 0 80 20 0 0 0"],
        "/proc/stat": [
            "cpu  100 0 100 1800 0 0 0 0 0 0\n",
            "cpu  200 0 200 2000 0 0 0 0 0 0\n",
        ],
    }

    def read_text(path: Path) -> str:
        try:
            return reads[str(path)].pop(0)
        except KeyError as exc:
            raise OSError(str(path)) from exc

    sampler = ProcessCpuUsageSampler(cpu_count=8, read_text=read_text)

    assert sampler.sample(123) is None
    assert sampler.sample(456) is None


def test_process_cpu_usage_sampler_reports_peak_thread_pct(tmp_path) -> None:
    proc_dir = tmp_path / "123"
    task_dir = proc_dir / "task"
    thread_a = task_dir / "123"
    thread_b = task_dir / "124"
    thread_a.mkdir(parents=True)
    thread_b.mkdir(parents=True)

    stat_path = tmp_path / "stat"
    process_stat = proc_dir / "stat"
    thread_a_stat = thread_a / "stat"
    thread_b_stat = thread_b / "stat"

    stat_path.write_text("cpu  0 0 0 0 0 0 0 0 0 0\n", encoding="utf-8")
    process_stat.write_text(
        "123 (Game.exe) S 1 1 1 0 0 0 0 0 0 0 20 10 0 0 0",
        encoding="utf-8",
    )
    thread_a_stat.write_text(
        "123 (Main Thread) S 1 1 1 0 0 0 0 0 0 0 20 10 0 0 0",
        encoding="utf-8",
    )
    thread_b_stat.write_text(
        "124 (Worker) S 1 1 1 0 0 0 0 0 0 0 5 5 0 0 0",
        encoding="utf-8",
    )

    sampler = ProcessCpuUsageSampler(proc_root=tmp_path, cpu_count=4)

    assert sampler.sample_usage(123).process_util_pct is None

    stat_path.write_text("cpu  100 0 100 200 0 0 0 0 0 0\n", encoding="utf-8")
    process_stat.write_text(
        "123 (Game.exe) S 1 1 1 0 0 0 0 0 0 0 100 60 0 0 0",
        encoding="utf-8",
    )
    thread_a_stat.write_text(
        "123 (Main Thread) S 1 1 1 0 0 0 0 0 0 0 95 30 0 0 0",
        encoding="utf-8",
    )
    thread_b_stat.write_text(
        "124 (Worker) S 1 1 1 0 0 0 0 0 0 0 35 10 0 0 0",
        encoding="utf-8",
    )

    usage = sampler.sample_usage(123)

    assert usage.process_util_pct == 32
    assert usage.peak_thread_util_pct == 95
    assert usage.peak_thread_id == 123
