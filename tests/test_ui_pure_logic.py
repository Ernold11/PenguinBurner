"""Coverage for UI helper modules that hold pure logic (no live Qt needed).

Covers ui/verify.py, the ui/lact_export.py config parsers, and ui/error_reporting
(driven through fake controls/log_view + monkeypatched dialog functions).
"""

from __future__ import annotations

import ui.error_reporting as error_reporting
import ui.lact_export as lact_export
from ui.verify import elapsed_from_line, progress_percent, workload_label


# --- ui/verify.py -------------------------------------------------------------


def test_elapsed_from_line_parses_and_floors_at_zero() -> None:
    assert elapsed_from_line("status elapsed=12.5s remaining") == 12.5
    assert elapsed_from_line("elapsed=0s") == 0.0


def test_elapsed_from_line_returns_none_without_match() -> None:
    assert elapsed_from_line("no timing here") is None
    assert elapsed_from_line("") is None
    assert elapsed_from_line(None) is None


def test_progress_percent_clamps_to_0_100() -> None:
    assert progress_percent(0, 600) == 0
    assert progress_percent(300, 600) == 50
    assert progress_percent(9999, 600) == 100


def test_progress_percent_returns_zero_on_bad_input() -> None:
    assert progress_percent(None, 600) == 0
    assert progress_percent("x", "y") == 0


def test_workload_label_covers_every_combination() -> None:
    assert workload_label(q2rtx_enabled=True, cuda_enabled=True) == (
        "Q2RTX timedemo and CUDA compute test"
    )
    assert workload_label(q2rtx_enabled=True, cuda_enabled=False) == "Q2RTX timedemo"
    assert workload_label(q2rtx_enabled=False, cuda_enabled=True) == "CUDA compute test"
    assert workload_label(q2rtx_enabled=False, cuda_enabled=False) == "No workload"


# --- ui/lact_export.py parsers ------------------------------------------------


def test_lact_gpu_id_from_config_reads_first_gpu_key(tmp_path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join(
            [
                "# a comment",
                "daemon:",
                "  log_level: info",
                "gpus:",
                "  1002:73df-1043:8888:",
                "    fan_control_enabled: true",
            ]
        ),
        encoding="utf-8",
    )
    assert lact_export.lact_gpu_id_from_config(config) == "1002:73df-1043:8888"


def test_lact_gpu_id_from_config_returns_empty_when_no_gpus_block(tmp_path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("daemon:\n  log_level: info\n", encoding="utf-8")
    assert lact_export.lact_gpu_id_from_config(config) == ""


def test_lact_gpu_id_from_config_stops_when_block_ends(tmp_path) -> None:
    # A non-indented line after gpus: ends the block before any id is found.
    config = tmp_path / "config.yaml"
    config.write_text("gpus:\nother: 1\n", encoding="utf-8")
    assert lact_export.lact_gpu_id_from_config(config) == ""


def test_lact_gpu_id_from_config_missing_file_returns_empty(tmp_path) -> None:
    assert lact_export.lact_gpu_id_from_config(tmp_path / "nope.yaml") == ""


def test_detect_lact_gpu_id_prefers_output_dir(tmp_path) -> None:
    (tmp_path / "config.yaml").write_text(
        "gpus:\n  abcd:0001:\n    x: 1\n", encoding="utf-8"
    )
    assert lact_export.detect_lact_gpu_id(tmp_path) == "abcd:0001"


def test_detect_lact_gpu_id_empty_when_nothing_found(tmp_path, monkeypatch) -> None:
    # Force every candidate path to yield no id, independent of the host's /etc/lact.
    monkeypatch.setattr(lact_export, "lact_gpu_id_from_config", lambda _path: "")
    assert lact_export.detect_lact_gpu_id(tmp_path) == ""


def test_lact_export_output_path_appends_filename(tmp_path) -> None:
    assert lact_export.lact_export_output_path(tmp_path) == tmp_path / "config.yaml"


def test_profile_is_afterburner_flag() -> None:
    assert lact_export._profile_is_afterburner({"runtime_source": "afterburner"}) is True
    assert lact_export._profile_is_afterburner({"runtime_source": "auto-uv"}) is False
    assert lact_export._profile_is_afterburner({}) is False


def test_current_fan_config_returns_empty_on_load_error(monkeypatch) -> None:
    def _boom(_path):
        raise RuntimeError("no config")

    monkeypatch.setattr(lact_export, "load_config", _boom)
    assert lact_export.current_fan_config() == {}


def test_current_fan_config_extracts_fan_mapping(monkeypatch) -> None:
    monkeypatch.setattr(
        lact_export, "load_config", lambda _path: {"fan": {"enabled": True}}
    )
    assert lact_export.current_fan_config() == {"enabled": True}


def test_current_fan_config_handles_non_dict_fan(monkeypatch) -> None:
    monkeypatch.setattr(lact_export, "load_config", lambda _path: {"fan": "nope"})
    assert lact_export.current_fan_config() == {}


# --- ui/error_reporting.py ----------------------------------------------------


class _FakeControls:
    def __init__(self) -> None:
        self.status_text = None

    def set_status_text(self, text: str) -> None:
        self.status_text = text


class _FakeTextEdit:
    def __init__(self, text: str) -> None:
        self._text = text

    def toPlainText(self) -> str:
        return self._text


class _FakeLogView:
    def __init__(self, text: str = "") -> None:
        self.appended: list[str] = []
        self.text_edit = _FakeTextEdit(text)

    def append(self, text: str) -> None:
        self.appended.append(text)


def _reporter(monkeypatch, *, log_text: str = ""):
    calls: list[dict] = []
    monkeypatch.setattr(
        error_reporting, "show_error_dialog", lambda **kw: calls.append(kw)
    )
    monkeypatch.setattr(
        error_reporting,
        "process_failure_details",
        lambda **kw: f"details:{kw['action_label']}:{kw['exit_code']}",
    )
    monkeypatch.setattr(error_reporting, "qt_enum_name", lambda status: f"status:{status}")
    controls = _FakeControls()
    log_view = _FakeLogView(log_text)
    reporter = error_reporting.ErrorReporter(
        QtCore=object(),
        QtGui=object(),
        QtWidgets=object(),
        parent=object(),
        controls=controls,
        log_view=log_view,
    )
    return reporter, controls, log_view, calls


def test_error_reporter_show_sets_status_logs_and_opens_dialog(monkeypatch) -> None:
    reporter, controls, log_view, calls = _reporter(monkeypatch)

    reporter.show("VF Curve", "Something failed\nsecond line")

    assert controls.status_text == "Something failed"
    assert any("VF Curve: Something failed" in entry for entry in log_view.appended)
    assert calls and calls[0]["title"] == "VF Curve"


def test_error_reporter_show_process_includes_details(monkeypatch) -> None:
    reporter, _controls, _log_view, calls = _reporter(monkeypatch, log_text="line\n" * 5)

    reporter.show_process(
        title="Scan failed",
        action_label="Auto-UV scan",
        exit_code=3,
        exit_status="crash",
    )

    assert calls[0]["title"] == "Scan failed"
    assert calls[0]["details"] == "details:Auto-UV scan:3"


def test_recent_log_tail_limits_lines_and_chars(monkeypatch) -> None:
    reporter, _controls, _log_view, _calls = _reporter(
        monkeypatch, log_text="\n".join(f"line{i}" for i in range(200))
    )

    tail = reporter.recent_log_tail(lines=10, char_limit=12000)
    assert tail.splitlines() == [f"line{i}" for i in range(190, 200)]

    clipped = reporter.recent_log_tail(lines=200, char_limit=20)
    assert len(clipped) == 20
