"""Starting, watching and stopping a Heroic game."""

from __future__ import annotations

import json
import subprocess

from integrations.heroic import process
from integrations.launchers import host_process as host


def _installed(monkeypatch, *, native: bool = True):
    monkeypatch.setattr(
        process, "host_has_command", lambda name: native or name == "flatpak"
    )
    monkeypatch.setattr(
        process, "run_on_host", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0)
    )


def test_a_game_is_started_through_heroics_own_launch_url(monkeypatch) -> None:
    """Heroic builds the command from the game's config, wrappers included."""
    _installed(monkeypatch)
    started: list[list[str]] = []
    monkeypatch.setattr(process, "start_on_host", lambda cmd: started.append(cmd) or True)

    assert process.launch_heroic_game("legendary", "Turkey") is True
    assert started == [
        ["heroic", "--no-gui", "heroic://launch/legendary/Turkey?gui=false"]
    ]


def test_the_launch_url_asks_heroic_to_stay_out_of_the_way(monkeypatch) -> None:
    """A running Heroic parsed its own argv at startup, so the flag alone
    would raise its window over the game."""
    _installed(monkeypatch)

    command = process.launch_command("legendary", "Turkey")

    assert command is not None
    assert command[-1].endswith("?gui=false")


def test_a_flatpak_heroic_is_asked_the_same_thing(monkeypatch) -> None:
    _installed(monkeypatch, native=False)

    assert process.launch_command("gog", "1454") == [
        "flatpak",
        "run",
        "com.heroicgameslauncher.hgl",
        "--no-gui",
        "heroic://launch/gog/1454?gui=false",
    ]


def test_an_unusable_name_never_reaches_the_command_line(monkeypatch) -> None:
    _installed(monkeypatch)

    for runner, app_name in (("", "Turkey"), ("legendary", ""), ("legendary", "a/b")):
        assert process.launch_command(runner, app_name) is None


def test_running_sessions_are_read_from_the_host_probe(
    monkeypatch,
) -> None:
    """Session identities remain usable after the wrapper's exec."""
    monkeypatch.setattr(
        process,
        "run_on_host",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 0, json.dumps({"sessions": [
                (4210, "heroic:Turkey"), (4211, "heroic:Turkey"), (4300, "lutris:27")
            ], "unreadable": []})
        ),
    )

    assert process.running_heroic_games() == {"Turkey": (4210, 4211)}


def test_a_failed_probe_says_so_instead_of_reporting_nothing_running(
    monkeypatch,
) -> None:
    monkeypatch.setattr(process, "run_on_host", lambda *args, **kwargs: None)

    assert process.running_heroic_games() is None


def test_a_game_id_with_a_space_survives_the_host_probe(monkeypatch) -> None:
    """The environment carries the decoded game key, not its flag encoding."""
    monkeypatch.setattr(
        process,
        "run_on_host",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 0, json.dumps({"sessions": [(9, "heroic:Sid Meier")], "unreadable": []})
        ),
    )

    assert process.running_heroic_games() == {"Sid Meier": (9,)}


def test_stopping_signals_the_wrapper_that_became_the_session(monkeypatch) -> None:
    """The wrapper execs the game, so its pid is the session's own."""
    sent: list[int] = []
    monkeypatch.setattr(host, "running_in_flatpak", lambda: False)
    monkeypatch.setattr(host.os, "kill", lambda pid, _sig: sent.append(pid))

    assert process.stop_heroic_game(4210) is True
    assert sent == [4210]
