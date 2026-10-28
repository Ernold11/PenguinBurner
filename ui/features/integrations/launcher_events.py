"""Qt delivery of launcher-session, filesystem and worker completion events."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Iterable
from pathlib import Path

from PySide6 import QtCore, QtNetwork

from runtime.daemon_client import DAEMON_SOCKET_ENV, DEFAULT_DAEMON_SOCKET


class WorkerEvents(QtCore.QObject):
    completed = QtCore.Signal(object, object)

    def __init__(self, parent: QtCore.QObject) -> None:
        super().__init__(parent)
        self.completed.connect(self._deliver, QtCore.Qt.ConnectionType.QueuedConnection)

    def worker(self, work: Callable[[], None], done: Callable[[], None]) -> threading.Thread:
        def run() -> None:
            try:
                work()
            finally:
                try:
                    self.completed.emit(threading.current_thread(), done)
                except RuntimeError:
                    pass  # The owning window was already destroyed.

        return threading.Thread(target=run, daemon=True)

    @QtCore.Slot(object, object)
    def _deliver(self, worker: threading.Thread, done: Callable[[], None]) -> None:
        # The signal is the worker's final action. Joining here merely releases
        # its Python Thread bookkeeping; no task or subprocess is still pending.
        worker.join()
        done()


class LauncherEvents(QtCore.QObject):
    snapshot = QtCore.Signal(dict)
    disconnected = QtCore.Signal()
    library_changed = QtCore.Signal()

    def __init__(self, parent: QtCore.QObject, *, socket_path: str | None = None) -> None:
        super().__init__(parent)
        self.socket_path = socket_path or os.environ.get(DAEMON_SOCKET_ENV) or DEFAULT_DAEMON_SOCKET
        self.socket = QtNetwork.QLocalSocket(self)
        self.socket.connected.connect(self._connected)
        self.socket.readyRead.connect(self._read)
        self.socket.disconnected.connect(self._disconnected)
        self.socket.errorOccurred.connect(self._disconnected)
        self.retry = QtCore.QTimer(self)
        self.retry.setSingleShot(True)
        self.retry.timeout.connect(self._connect)
        self.handshake = QtCore.QTimer(self)
        self.handshake.setSingleShot(True)
        self.handshake.timeout.connect(self._handshake_expired)
        self.retry_ms = 1000
        self.buffer = bytearray()
        self.epoch = ""
        self.sequence = -1
        self.available = False
        self.started = False
        self.watcher = QtCore.QFileSystemWatcher(self)
        self.watcher.fileChanged.connect(self._changed)
        self.watcher.directoryChanged.connect(self._changed)
        self.paths: tuple[Path, ...] = ()
        self._path_state: dict[Path, tuple[int, int] | None] = {}

    def start(self) -> None:
        if not self.started:
            self.started = True
            self._connect()

    def _connect(self) -> None:
        self.buffer.clear()
        self.socket.abort()
        self.socket.connectToServer(self.socket_path)
        self.handshake.start(5000)  # Transport health only; never game state.

    def _connected(self) -> None:
        self.buffer.clear()
        self.epoch, self.sequence = "", -1
        self.socket.write(b'{"method":"subscribe_launcher_sessions"}\n')

    def _read(self) -> None:
        self.buffer.extend(self.socket.readAll().data())
        if len(self.buffer) > 1024 * 1024:
            self.socket.abort()
            self._disconnected()
            return
        while b"\n" in self.buffer:
            line, _, rest = self.buffer.partition(b"\n")
            self.buffer = bytearray(rest)
            try:
                response = json.loads(line)
                if not response.get("ok"):
                    raise ValueError("session stream refused")
                data = response["result"]
                epoch, sequence = data["epoch"], data["sequence"]
                if (not isinstance(epoch, str) or not isinstance(sequence, int)
                        or not isinstance(data["sessions"], list)
                        or not all(isinstance(s, dict) and isinstance(s.get("app_id"), str)
                                   for s in data["sessions"])
                        or not isinstance(data.get("ended", []), list)
                        or not all(isinstance(e, dict) and isinstance(e.get("session"), dict)
                                   and isinstance(e.get("sequence"), int)
                                   for e in data.get("ended", []))):
                    raise TypeError("invalid session snapshot")
            except (ValueError, KeyError, TypeError, AttributeError):
                self.socket.abort()
                self._disconnected()
                return
            if self.epoch and epoch != self.epoch:
                self.socket.abort()
                self._disconnected()
                return
            if epoch == self.epoch and sequence <= self.sequence:
                continue
            # Each message replaces the complete snapshot; coalesced sequence
            # numbers are safe, and a new daemon epoch starts a fresh history.
            self.epoch, self.sequence = epoch, sequence
            self.handshake.stop()
            self.available = True
            self.retry_ms = 1000
            self.retry.stop()
            self.snapshot.emit(data)

    def _handshake_expired(self) -> None:
        self.socket.abort()
        self._disconnected()

    def _disconnected(self, *_args: object) -> None:
        self.handshake.stop()
        if self.available:
            self.available = False
            self.disconnected.emit()
        if self.started and not self.retry.isActive():
            self.retry.start(self.retry_ms)
            self.retry_ms = min(self.retry_ms * 2, 30000)

    def watch(self, paths: Iterable[Path]) -> None:
        self.paths = tuple(paths)
        self._path_state = self._fingerprints()
        self._rewatch()

    def _rewatch(self) -> None:
        existing = set(self.watcher.files()) | set(self.watcher.directories())
        wanted: set[str] = set()
        for path in self.paths:
            # Parent watches survive atomic replacement and notice creation of
            # a store/config directory that did not exist at initial scan.
            candidate = path
            while not candidate.exists() and candidate != candidate.parent:
                candidate = candidate.parent
            wanted.add(str(candidate))
            if candidate.parent.exists():
                wanted.add(str(candidate.parent))
        if existing - wanted:
            self.watcher.removePaths(sorted(existing - wanted))
        if wanted - existing:
            self.watcher.addPaths(sorted(wanted - existing))

    def _fingerprints(self) -> dict[Path, tuple[int, int] | None]:
        result = {}
        for path in self.paths:
            try:
                stat = path.stat()
                result[path] = (stat.st_mtime_ns, stat.st_ino)
            except OSError:
                result[path] = None
        return result

    def _changed(self, _path: str) -> None:
        self._rewatch()
        current = self._fingerprints()
        if current != self._path_state:
            self._path_state = current
            self.library_changed.emit()
