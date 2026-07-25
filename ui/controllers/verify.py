from __future__ import annotations

from pathlib import Path
import json
import os
import signal

from ui.features.tuning.verify import elapsed_from_line
from ui.features.tuning.verify import progress_percent


class VerifyController:
    def __init__(self, *, QtCore, parent=None, stop_request_path: Path):
        self.QtCore = QtCore
        self.parent = parent
        self.stop_request_path = stop_request_path
        self.process = None
        self.target_duration_s = 0
        self.last_elapsed_s = 0.0
        self.stop_requested = False
        self._buffer = ""
        self.on_output = lambda _text: None
        self.on_telemetry = lambda _payload: None
        self.on_progress = lambda _percent, _elapsed_s, _target_s, _detail: None
        self.on_finished = lambda _exit_code, _exit_status, _stopped: None

    def is_running(self) -> bool:
        return self.process is not None

    def start(self, command: list[str], *, duration_s: int) -> bool:
        if self.process is not None or not command:
            return False
        # Verify is a long-running command with progress parsed from stdout.
        self.target_duration_s = max(1, int(duration_s))
        self.last_elapsed_s = 0.0
        self.stop_requested = False
        self._buffer = ""
        self.stop_request_path.parent.mkdir(parents=True, exist_ok=True)
        self.stop_request_path.unlink(missing_ok=True)
        process = self.QtCore.QProcess(self.parent)
        process.setProcessChannelMode(self.QtCore.QProcess.MergedChannels)
        process.readyReadStandardOutput.connect(self._read_output)
        process.finished.connect(self._finished)
        self.process = process
        process.start(command[0], command[1:])
        if process.waitForStarted(3000):
            return True
        self.on_output("Failed to start profile verification.\n")
        self._finished(-1, self.QtCore.QProcess.CrashExit)
        return False

    def stop(self) -> None:
        if self.process is None:
            return
        self.stop_requested = True
        self.stop_request_path.parent.mkdir(parents=True, exist_ok=True)
        self.stop_request_path.write_text(
            "stop requested by PenguinBurner UI\n",
            encoding="utf-8",
        )
        self.on_output(f"\nRequested cooperative profile verification stop: {self.stop_request_path}\n")
        pid = int(self.process.processId())
        if pid > 0:
            try:
                os.kill(pid, signal.SIGINT)
                self.on_output("Sent SIGINT to profile verification launcher.\n")
            except OSError:
                self.process.terminate()
        else:
            self.process.terminate()
        self.QtCore.QTimer.singleShot(30000, self.kill_if_running)

    def kill_if_running(self) -> None:
        if (
            self.process is not None
            and self.process.state() != self.QtCore.QProcess.NotRunning
        ):
            self.process.kill()

    def _read_output(self) -> None:
        if self.process is None:
            return
        data = bytes(self.process.readAllStandardOutput()).decode(
            "utf-8",
            errors="replace",
        )
        if not data:
            return
        # load_telemetry JSON lines are already rendered as a live plot marker,
        # so keep them out of the human-visible log (mirrors ScanController).
        self._buffer += data
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._handle_line(line)

    def _handle_line(self, line: str) -> None:
        stripped = line.strip()
        if stripped.startswith("{"):
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict) and payload.get("event") == "load_telemetry":
                self.on_telemetry(payload)
                return
        self.on_output(line + "\n")
        elapsed = elapsed_from_line(line)
        if elapsed is None:
            return
        self.last_elapsed_s = max(self.last_elapsed_s, elapsed)
        self.on_progress(
            progress_percent(self.last_elapsed_s, self.target_duration_s),
            self.last_elapsed_s,
            self.target_duration_s,
            stripped,
        )

    def _finished(self, exit_code, exit_status) -> None:
        process = self.process
        # Drain anything still buffered in the pipe so a final trailing
        # progress/telemetry line emitted just before exit is not lost.
        if process is not None:
            try:
                tail = bytes(process.readAllStandardOutput()).decode(
                    "utf-8", errors="replace"
                )
            except RuntimeError:
                tail = ""
            if tail:
                self._buffer += tail
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._handle_line(line)
        if self._buffer.strip():
            self._handle_line(self._buffer)
        self._buffer = ""
        stopped = bool(self.stop_requested)
        self.process = None
        self.stop_requested = False
        self.stop_request_path.unlink(missing_ok=True)
        self.on_finished(exit_code, exit_status, stopped)
        if process is not None:
            process.deleteLater()
