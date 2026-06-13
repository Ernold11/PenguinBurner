from __future__ import annotations

from pathlib import Path
import json
import os
import signal


class ScanController:
    def __init__(self, *, QtCore, parent=None, stop_request_path: Path | None = None):
        self.QtCore = QtCore
        self.parent = parent
        self.stop_request_path = stop_request_path
        self.process = None
        self.stop_requested = False
        self._buffer = ""
        self.on_output = lambda _text: None
        self.on_event = lambda _payload: None
        self.on_human_line = lambda _line: None
        self.on_finished = lambda _exit_code, _exit_status, _stopped: None

    def is_running(self) -> bool:
        return self.process is not None

    def start(self, command: list[str]) -> bool:
        if self.process is not None or not command:
            return False
        self.stop_requested = False
        self._buffer = ""
        if self.stop_request_path is not None:
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
        self.on_output("Failed to start Auto-UV process.\n")
        self._finished(-1, self.QtCore.QProcess.CrashExit)
        return False

    def stop(self) -> None:
        if self.process is None:
            return
        self.stop_requested = True
        if self.stop_request_path is not None:
            self.stop_request_path.parent.mkdir(parents=True, exist_ok=True)
            self.stop_request_path.write_text(
                "stop requested by PenguinBurner UI\n",
                encoding="utf-8",
            )
            self.on_output(f"\nRequested cooperative Auto-UV stop: {self.stop_request_path}\n")
        pid = int(self.process.processId())
        if pid > 0:
            try:
                os.kill(pid, signal.SIGINT)
                self.on_output("Sent SIGINT to Auto-UV launcher.\n")
            except OSError:
                self.process.terminate()
        else:
            self.process.terminate()
        self.QtCore.QTimer.singleShot(30000, self.kill_if_running)

    def write_json_response(self, path: str | Path, payload: dict) -> None:
        response_path = Path(path).expanduser()
        if not str(response_path).strip():
            return
        response_path.parent.mkdir(parents=True, exist_ok=True)
        response_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def kill_if_running(self) -> None:
        if (
            self.process is not None
            and self.process.state() != self.QtCore.QProcess.NotRunning
        ):
            self.process.kill()

    def _read_output(self) -> None:
        process = self.process
        if process is None:
            return
        try:
            chunk = process.readAllStandardOutput()
        except RuntimeError:
            # A readyRead signal can be delivered after the QProcess has been
            # retired/deleted (e.g. right after a stop). The underlying C++
            # object is gone, so there is nothing left to read.
            return
        data = bytes(chunk).decode(
            "utf-8",
            errors="replace",
        )
        if not data:
            return
        self.on_output(data)
        # CLI JSON events are line-delimited, but QProcess chunks are not.
        self._buffer += data
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._handle_line(line)

    def _handle_line(self, line: str) -> None:
        stripped = line.strip()
        if not stripped:
            return
        if not stripped.startswith("{"):
            self.on_human_line(stripped)
            return
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            self.on_human_line(stripped)
            return
        if isinstance(payload, dict):
            self.on_event(payload)

    def _finished(self, exit_code, exit_status) -> None:
        process = self.process
        # Drain anything still buffered in the pipe so trailing JSON events
        # (e.g. a final_choice_request emitted just before exit) are not lost.
        if process is not None:
            try:
                tail = bytes(process.readAllStandardOutput()).decode(
                    "utf-8", errors="replace"
                )
            except RuntimeError:
                tail = ""
            if tail:
                self.on_output(tail)
                self._buffer += tail
        if self._buffer.strip():
            self._handle_line(self._buffer)
        self._buffer = ""
        self.process = None
        if process is not None:
            # Detach the slots so a late readyRead/finished can never fire on a
            # retired process (which would touch a deleted C++ object).
            try:
                process.readyReadStandardOutput.disconnect(self._read_output)
                process.finished.disconnect(self._finished)
            except (RuntimeError, TypeError):
                pass
        stopped = bool(self.stop_requested)
        self.stop_requested = False
        self.on_finished(exit_code, exit_status, stopped)
        if process is not None:
            process.deleteLater()
