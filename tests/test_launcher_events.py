"""Real Qt socket delivery, filesystem replacement and launcher transport failures."""
from __future__ import annotations

import json

import pytest
from PySide6 import QtCore, QtNetwork

from integrations.launchers import session
from ui.features.integrations.launcher_events import LauncherEvents


def _send(peer, *, epoch="one", sequence=1, sessions=None):
    peer.write(json.dumps({"ok": True, "result": {
        "epoch": epoch, "sequence": sequence, "sessions": sessions or [],
    }}).encode() + b"\n")
    peer.flush()


def test_atomic_snapshot_order_and_reconnect(qtbot, tmp_path):
    parent = QtCore.QObject()
    server = QtNetwork.QLocalServer(parent)
    path = str(tmp_path / "events.sock")
    assert server.listen(path)
    events = LauncherEvents(parent, socket_path=path)
    snapshots, losses = [], []
    events.snapshot.connect(snapshots.append)
    events.disconnected.connect(lambda: losses.append(True))
    events.start()
    qtbot.waitUntil(server.hasPendingConnections)
    peer = server.nextPendingConnection()
    qtbot.waitUntil(lambda: peer.bytesAvailable() > 0)
    assert json.loads(peer.readLine().data())["method"] == "subscribe_launcher_sessions"
    _send(peer, sequence=3, sessions=[{"app_id": "heroic:game", "wrapped": True}])
    _send(peer, sequence=2)
    _send(peer, sequence=3)
    _send(peer, sequence=5)
    qtbot.waitUntil(lambda: len(snapshots) == 2)
    assert [s["sequence"] for s in snapshots] == [3, 5]
    peer.abort()
    qtbot.waitUntil(lambda: bool(losses))
    assert not events.available
    # Exercise a reconnect without making correctness depend on backoff time.
    events.retry.stop()
    events._connect()
    qtbot.waitUntil(server.hasPendingConnections)
    peer = server.nextPendingConnection()
    qtbot.waitUntil(lambda: peer.bytesAvailable() > 0)
    peer.readAll()
    _send(peer, epoch="two", sequence=0, sessions=[{"app_id": "steam:620"}])
    qtbot.waitUntil(lambda: len(snapshots) == 3)
    assert snapshots[-1]["epoch"] == "two"
    assert events.available
    events.started = False
    events.socket.abort()


def test_incomplete_and_malformed_stream_does_not_publish(qtbot, tmp_path):
    parent = QtCore.QObject()
    server = QtNetwork.QLocalServer(parent)
    path = str(tmp_path / "events.sock")
    assert server.listen(path)
    events = LauncherEvents(parent, socket_path=path)
    snapshots = []
    events.snapshot.connect(snapshots.append)
    events.start()
    qtbot.waitUntil(server.hasPendingConnections)
    peer = server.nextPendingConnection()
    peer.write(b'{"ok":true,')
    peer.flush()
    qtbot.waitUntil(lambda: bool(events.buffer))
    assert snapshots == []
    peer.write(b'"result":{"epoch":"a","sequence":1,"sessions":[null]}}\n')
    peer.flush()
    qtbot.waitUntil(events.retry.isActive)
    assert snapshots == []
    assert not events.available
    events.started = False
    events.retry.stop()
    events.socket.abort()


def test_atomic_file_replacement_and_missing_directory(qtbot, tmp_path):
    parent = QtCore.QObject()
    events = LauncherEvents(parent)
    changes = []
    events.library_changed.connect(lambda: changes.append(True))
    target = tmp_path / "store" / "installed.json"
    events.watch([target])
    target.parent.mkdir()
    target.write_text("{}")
    qtbot.waitUntil(lambda: len(changes) >= 1)
    changes.clear()
    replacement = target.with_suffix(".new")
    replacement.write_text('{"new-game": {}}')
    replacement.replace(target)
    qtbot.waitUntil(lambda: len(changes) >= 1)
    assert str(target) in events.watcher.files()


@pytest.mark.parametrize("key", ["steam:620", "lutris:27", "heroic:Ghostwire"])
def test_each_launcher_registers_without_requiring_a_profile(monkeypatch, key):
    calls = []
    def register(app, identity):
        calls.append((app, identity))
        return {"pid": 12345}
    monkeypatch.setattr(session, "register_launcher_session", register)
    env = {"PENGUIN_BURNER_GAME_KEY": key}
    identity, host_pid = session.begin_session(env)
    assert calls == [(key, identity)]
    assert env["PENGUIN_BURNER_TELEMETRY_SESSION"] == "12345"
    assert identity == env[session.SESSION_ID_ENV]
    assert host_pid == 12345
    assert session.begin_session(env)[0] != identity


def test_daemon_unavailable_keeps_recoverable_launch_identity(monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("service stopped")
    monkeypatch.setattr(session, "register_launcher_session", unavailable)
    monkeypatch.setattr(session, "update_launcher_session", unavailable)
    env = {"SteamAppId": "620"}
    identity, host_pid = session.begin_session(env)
    assert host_pid is None
    session.report_session(identity, phase="running")
    assert env["PENGUIN_BURNER_GAME_KEY"] == "steam:620"
    assert env[session.SESSION_ID_ENV] == identity
