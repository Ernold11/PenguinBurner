from __future__ import annotations

import shlex
from typing import Callable


class CommandController:
    def __init__(self, *, QtCore, parent=None):
        self.QtCore = QtCore
        self.parent = parent
        self.process = None
        self.kind = ""
        self.command: list[str] = []
        self.on_output: Callable[..., None] = lambda _text: None
        self.on_finished: Callable[..., None] = (
            lambda _kind, _exit_code, _exit_status: None
        )

    def is_running(self) -> bool:
        return self.process is not None

    def start(self, kind: str, command: list[str], *, fail_text: str = "") -> bool:
        if self.process is not None or not command:
            return False
        # One controller instance owns one short-lived profile command at a time.
        process = self.QtCore.QProcess(self.parent)
        process.setProcessChannelMode(self.QtCore.QProcess.MergedChannels)
        process.readyReadStandardOutput.connect(self._read_output)
        process.finished.connect(self._finished)
        self.process = process
        self.kind = str(kind)
        self.command = list(command)
        self.on_output("\n$ " + " ".join(shlex.quote(part) for part in command) + "\n")
        process.start(command[0], command[1:])
        if process.waitForStarted(3000):
            return True
        self.on_output((fail_text or "Failed to start command.") + "\n")
        self._finished(-1, self.QtCore.QProcess.CrashExit)
        return False

    def _read_output(self) -> None:
        if self.process is None:
            return
        data = bytes(self.process.readAllStandardOutput()).decode(
            "utf-8",
            errors="replace",
        )
        if data:
            self.on_output(data)

    def _finished(self, exit_code, exit_status) -> None:
        process = self.process
        kind = self.kind
        self.process = None
        self.kind = ""
        self.command = []
        self.on_finished(kind, exit_code, exit_status)
        if process is not None:
            process.deleteLater()
