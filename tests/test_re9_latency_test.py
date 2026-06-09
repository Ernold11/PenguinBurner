from pathlib import Path

import latency_telemetry.re9_latency_test as re9_latency_test
from latency_telemetry.steam_launch_check import CompatToolCheck, LaunchOptionsCheck


def test_re9_latency_test_refuses_bad_launch_config(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        re9_latency_test,
        "check_launch_options",
        lambda **_kwargs: LaunchOptionsCheck(
            app_id="3764200",
            config_path=Path("/tmp/localconfig.vdf"),
            launch_options="OLD=1 %command%",
            required_tokens=("NEEDED=1",),
            missing_tokens=("NEEDED=1",),
        ),
    )
    monkeypatch.setattr(
        re9_latency_test,
        "check_compat_tool",
        lambda **_kwargs: CompatToolCheck(
            app_id="3764200",
            config_path=Path("/tmp/config.vdf"),
            expected_tool="Proton-CachyOS PB-Re9-Reflex",
            actual_tool="Proton-CachyOS PB-Re9-Reflex",
        ),
    )

    assert re9_latency_test.main(["--duration", "1"]) == 2

    output = capsys.readouterr().out
    assert "missing:" in output
    assert "error=RE9 launch config is not ready" in output


def test_re9_latency_test_captures_when_launch_config_is_ready(
    tmp_path, monkeypatch, capsys
) -> None:
    capture = tmp_path / "capture.log"
    seen: dict[str, object] = {}

    monkeypatch.setattr(
        re9_latency_test,
        "verify_re9_launch_config",
        lambda: (True, "launch-ok=True"),
    )

    def fake_capture(output_path, **kwargs) -> int:
        seen["output_path"] = output_path
        seen.update(kwargs)
        output_path.write_text(
            "2026-06-09 event=latency-meter gpu-render-p95=16.00ms\n",
            encoding="utf-8",
        )
        return 1

    monkeypatch.setattr(re9_latency_test, "capture_journal", fake_capture)

    assert (
        re9_latency_test.main(
            [
                "--output",
                str(capture),
                "--duration",
                "12.5",
                "--since",
                "now",
                "--no-sync",
                "--no-kernel-log",
            ]
        )
        == 0
    )

    assert seen["output_path"] == capture
    assert seen["duration_s"] == 12.5
    assert seen["since"] == "now"
    assert seen["follow"] is True
    assert seen["append"] is False
    assert seen["sync"] is False
    output = capsys.readouterr().out
    assert "launch-ok=True" in output
    assert f"capture={capture}" in output
    assert "captured_lines=1" in output
    assert "kernel_capture=" not in output


def test_re9_latency_test_captures_kernel_fault_sidecar(
    tmp_path, monkeypatch, capsys
) -> None:
    capture = tmp_path / "capture.log"
    seen: dict[str, object] = {}

    monkeypatch.setattr(
        re9_latency_test,
        "verify_re9_launch_config",
        lambda: (True, "launch-ok=True"),
    )

    def fake_capture(output_path, **kwargs) -> int:
        output_path.write_text(
            "2026-06-09 event=latency-meter gpu-render-p95=16.00ms\n",
            encoding="utf-8",
        )
        return 1

    class FakeStop:
        def set(self) -> None:
            seen["stopped"] = True

    class FakeThread:
        def join(self, timeout=None) -> None:
            seen["join_timeout"] = timeout

    def fake_kernel_capture(output_path, **kwargs):
        seen["kernel_output_path"] = output_path
        seen.update({f"kernel_{key}": value for key, value in kwargs.items()})
        output_path.write_text(
            "2026-06-09T22:16:19+02:00 home kernel: NVRM: Xid 8 re9.exe\n",
            encoding="utf-8",
        )
        return FakeThread(), FakeStop(), {"count": 1, "error": None}

    monkeypatch.setattr(re9_latency_test, "capture_journal", fake_capture)
    monkeypatch.setattr(
        re9_latency_test,
        "start_kernel_fault_capture",
        fake_kernel_capture,
    )

    assert (
        re9_latency_test.main(
            [
                "--output",
                str(capture),
                "--duration",
                "12.5",
                "--since",
                "now",
                "--no-sync",
            ]
        )
        == 0
    )

    expected_kernel_path = capture.with_suffix(".log.kernel.log")
    assert seen["kernel_output_path"] == expected_kernel_path
    assert seen["kernel_since"] == "now"
    assert seen["kernel_duration_s"] == 17.5
    assert seen["kernel_sync"] is False
    assert seen["stopped"] is True
    assert seen["join_timeout"] == 3.0
    output = capsys.readouterr().out
    assert f"kernel_capture={expected_kernel_path}" in output
    assert "kernel_fault_lines=1" in output


def test_is_kernel_fault_line_matches_nvidia_faults() -> None:
    assert re9_latency_test.is_kernel_fault_line("NVRM: Xid 8, name=re9.exe")
    assert re9_latency_test.is_kernel_fault_line("nvidia: GPU reset")
    assert re9_latency_test.is_kernel_fault_line(
        "NVRM: dmaAllocMapping_GM107: can't alloc VA space"
    )
    assert not re9_latency_test.is_kernel_fault_line("audit: service started")
    assert not re9_latency_test.is_kernel_fault_line(
        "r8169 0000:05:00.0 eth0: RTL8168h/8111h, XID 541, IRQ 96"
    )
    assert not re9_latency_test.is_kernel_fault_line(
        "NVRM: loading NVIDIA UNIX Open Kernel Module"
    )
    assert not re9_latency_test.is_kernel_fault_line(
        "umip: re9.exe[20936] ip:15e5f154f: SGDT instruction cannot be used"
    )


def test_kernel_journal_command_uses_live_only_for_now() -> None:
    assert re9_latency_test.kernel_journal_command("now") == [
        "journalctl",
        "-k",
        "-o",
        "short-iso",
        "--no-pager",
        "-n",
        "0",
        "-f",
    ]


def test_ready_re9_process_filter_rejects_steam_wrapper() -> None:
    assert not re9_latency_test._is_ready_re9_process(
        {
            "pid": 1,
            "cmdline": (
                "SteamLaunch AppId=3764200 -- proton waitforexitandrun "
                "/home/jp/.local/share/Steam/steamapps/common/"
                "RESIDENT EVIL requiem BIOHAZARD requiem/re9.exe"
            ),
        }
    )
    assert re9_latency_test._is_ready_re9_process(
        {
            "pid": 2,
            "cmdline": (
                "S:\\common\\RESIDENT EVIL requiem BIOHAZARD requiem\\re9.exe "
                "/WineDetectionEnabled:False"
            ),
        }
    )
    assert re9_latency_test.kernel_journal_command("15 min ago") == [
        "journalctl",
        "-k",
        "-o",
        "short-iso",
        "--no-pager",
        "--since",
        "15 min ago",
        "-f",
    ]
