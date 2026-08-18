from __future__ import annotations

from types import SimpleNamespace

import pytest

import runtime.daemon_client as daemon_client_module
import ui.daemon_setup as daemon_setup_module
from runtime.daemon_client import DaemonCompatibilityError
from ui.daemon_setup import ensure_daemon_ready_for_privileged_action


class _Buttons:
    Yes = 1
    No = 2


class _MessageBox:
    StandardButton = _Buttons
    answer = _Buttons.Yes
    questions = []
    criticals = []

    @classmethod
    def question(cls, *args):
        cls.questions.append(args)
        return cls.answer

    @classmethod
    def critical(cls, *args):
        cls.criticals.append(args)


class _QtWidgets:
    QMessageBox = _MessageBox


def test_daemon_setup_returns_true_when_daemon_already_reachable() -> None:
    calls = []

    ok = ensure_daemon_ready_for_privileged_action(
        QtWidgets=_QtWidgets,
        parent=None,
        log=lambda _message: None,
        action_label="Starting Auto-UV",
        status_check=lambda: {"state": "idle"},
        command_factory=lambda: calls.append("command") or ["unused"],
    )

    assert ok is True
    assert calls == []


def test_daemon_setup_prompts_and_runs_migration_when_missing() -> None:
    _MessageBox.answer = _Buttons.Yes
    _MessageBox.questions = []
    _MessageBox.criticals = []
    logs = []
    status_calls = {"count": 0}
    commands = []

    def status_check():
        status_calls["count"] += 1
        if status_calls["count"] == 1:
            raise RuntimeError("missing socket")
        return {"state": "idle"}

    def run_command(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="installed\n", stderr="")

    ok = ensure_daemon_ready_for_privileged_action(
        QtWidgets=_QtWidgets,
        parent=None,
        log=logs.append,
        action_label="Starting Auto-UV",
        status_check=status_check,
        command_factory=lambda: ["pkexec", "penguin-burner-cli", "--migrate-to-daemon-service"],
        run_command=run_command,
    )

    assert ok is True
    assert commands == [["pkexec", "penguin-burner-cli", "--migrate-to-daemon-service"]]
    assert _MessageBox.questions
    assert "install or repair" in _MessageBox.questions[0][2]
    assert "migrated once" in _MessageBox.questions[0][2]
    assert not _MessageBox.criticals
    assert any("--migrate-to-daemon-service" in message for message in logs)


def test_daemon_setup_default_check_enforces_protocol_and_capabilities(
    monkeypatch,
) -> None:
    _MessageBox.answer = _Buttons.Yes
    _MessageBox.questions = []
    _MessageBox.criticals = []
    handshakes = []

    monkeypatch.setattr(daemon_setup_module, "application_version", lambda: "0.7.9")

    def fake_require(*capabilities, **kwargs):
        handshakes.append((capabilities, kwargs))
        if len(handshakes) == 1:
            raise DaemonCompatibilityError(
                "PenguinBurner hardware service protocol mismatch: "
                "client=2, daemon=1"
            )
        return {"state": "idle"}

    monkeypatch.setattr(
        daemon_setup_module, "require_daemon_capabilities", fake_require
    )
    # Isolate from this host's real unit file; the registration check has
    # its own dedicated test below.
    monkeypatch.setattr(
        daemon_setup_module,
        "daemon_worker_registration_error",
        lambda: None,
    )
    commands = []

    ok = ensure_daemon_ready_for_privileged_action(
        QtWidgets=_QtWidgets,
        parent=None,
        log=lambda _message: None,
        action_label="Starting Auto-UV",
        required_capabilities=("scan-stream-v1",),
        command_factory=lambda: ["repair-daemon"],
        run_command=lambda command, **_kwargs: (
            commands.append(command),
            SimpleNamespace(returncode=0, stdout="updated\n", stderr=""),
        )[1],
    )

    # A stale-but-reachable daemon must land in the repair prompt (the #22
    # failure shape: scan dies at start against an old daemon), and the
    # post-repair re-check must use the same strict handshake.
    assert ok is True
    assert handshakes == [
        (("scan-stream-v1",), {"expected_version": "0.7.9"}),
        (("scan-stream-v1",), {"expected_version": "0.7.9"}),
    ]
    assert commands == [["repair-daemon"]]
    assert "needs a matching" in _MessageBox.questions[0][2]
    assert "protocol mismatch" in _MessageBox.questions[0][2]
    assert not _MessageBox.criticals


def test_daemon_setup_cancel_leaves_action_blocked() -> None:
    _MessageBox.answer = _Buttons.No
    _MessageBox.questions = []
    _MessageBox.criticals = []
    ran = []

    ok = ensure_daemon_ready_for_privileged_action(
        QtWidgets=_QtWidgets,
        parent=None,
        log=lambda _message: None,
        action_label="Starting Auto-UV",
        status_check=lambda: (_ for _ in ()).throw(RuntimeError("missing socket")),
        command_factory=lambda: ["unused"],
        run_command=lambda *_args, **_kwargs: ran.append(True),
    )

    assert ok is False
    assert _MessageBox.questions
    assert ran == []


def test_daemon_setup_repairs_stale_worker_registration(monkeypatch) -> None:
    """Healthy daemon + unit pointing at another install -> repair prompt.

    Capabilities cannot see a flatpak<->pip switch; the worker-registration
    check must route it into the same install-or-repair flow."""
    _MessageBox.answer = _Buttons.Yes
    _MessageBox.questions = []
    _MessageBox.criticals = []
    registration = {"calls": 0}

    monkeypatch.setattr(
        daemon_setup_module,
        "require_daemon_capabilities",
        lambda *capabilities, **kwargs: {"state": "idle"},
    )

    def fake_registration_error():
        registration["calls"] += 1
        if registration["calls"] == 1:
            return (
                "the hardware service spawns scan workers from "
                "/flatpak/penguin_burner.py, but this PenguinBurner runs "
                "from /site/penguin_burner.py"
            )
        return None

    monkeypatch.setattr(
        daemon_setup_module,
        "daemon_worker_registration_error",
        fake_registration_error,
    )
    commands = []

    ok = ensure_daemon_ready_for_privileged_action(
        QtWidgets=_QtWidgets,
        parent=None,
        log=lambda _message: None,
        action_label="Starting Auto-UV",
        command_factory=lambda: ["repair-daemon"],
        run_command=lambda command, **_kwargs: (
            commands.append(command),
            SimpleNamespace(returncode=0, stdout="repaired\n", stderr=""),
        )[1],
    )

    assert ok is True
    assert commands == [["repair-daemon"]]
    assert registration["calls"] == 2
    assert "install or repair" in _MessageBox.questions[0][2]
    assert "spawns scan workers from" in _MessageBox.questions[0][2]
    assert not _MessageBox.criticals


def test_daemon_capability_gate_rejects_wrong_release_version(monkeypatch) -> None:
    monkeypatch.setattr(
        daemon_client_module,
        "daemon_status",
        lambda **_kwargs: {
            "protocol_major": daemon_client_module.DAEMON_PROTOCOL_MAJOR,
            "capabilities": ["scan-stream-v1"],
            "version": "0.7.8",
        },
    )

    with pytest.raises(
        DaemonCompatibilityError,
        match=r"release mismatch: app=0\.7\.9, daemon=0\.7\.8",
    ):
        daemon_client_module.require_daemon_capabilities(
            "scan-stream-v1",
            expected_version="0.7.9",
        )


def test_daemon_capability_gate_accepts_matching_release_version(monkeypatch) -> None:
    status = {
        "protocol_major": daemon_client_module.DAEMON_PROTOCOL_MAJOR,
        "capabilities": ["scan-stream-v1"],
        "version": "0.7.9",
    }
    monkeypatch.setattr(
        daemon_client_module,
        "daemon_status",
        lambda **_kwargs: status,
    )

    assert (
        daemon_client_module.require_daemon_capabilities(
            "scan-stream-v1",
            expected_version="0.7.9",
        )
        is status
    )
