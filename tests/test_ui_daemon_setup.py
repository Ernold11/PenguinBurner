from __future__ import annotations

from types import SimpleNamespace

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
