"""Refusing Play while a store client runs in the game's prefix unwrapped."""

from __future__ import annotations

import pytest

from integrations.launchers import prefix_guard


@pytest.fixture
def prefix_and_proc(tmp_path):
    prefix = tmp_path / "ea-app"
    prefix.mkdir()
    (prefix / "pfx").symlink_to(".")  # what Proton leaves in a Faugus prefix
    proc = tmp_path / "proc"

    def running(pid: int, command: str, env: dict[str, str]) -> None:
        (proc / str(pid)).mkdir(parents=True)
        (proc / str(pid) / "cmdline").write_bytes(command.encode() + b"\0--flag\0")
        (proc / str(pid) / "environ").write_bytes(
            b"".join(f"{key}={value}".encode() + b"\0" for key, value in env.items())
        )

    return prefix, proc, running


def test_reports_programs_in_that_prefix_that_we_did_not_start(prefix_and_proc, tmp_path):
    prefix, proc, running = prefix_and_proc
    running(10, r"C:\Program Files\Electronic Arts\EA Desktop\EADesktop.exe", {"WINEPREFIX": f"{prefix}/pfx/"})
    running(11, r"C:\Games\NFS\NFS11Remastered.exe",
            {"WINEPREFIX": str(prefix), "PENGUIN_BURNER_GAME_KEY": "faugus:nfs"})
    running(12, r"C:\Program Files (x86)\Battle.net\Battle.net.exe",
            {"WINEPREFIX": str(tmp_path / "other")})
    running(13, r"C:\windows\system32\services.exe", {"WINEPREFIX": str(prefix)})
    running(14, "/usr/bin/python3", {"WINEPREFIX": str(prefix)})

    assert prefix_guard.unwrapped_programs(str(prefix), proc_root=str(proc)) == ("EADesktop.exe",)
    assert prefix_guard.unwrapped_programs("", proc_root=str(proc)) == ()


def test_store_clients_are_named_and_unknown_programs_keep_their_name():
    assert prefix_guard.client_names(("EADesktop.exe", "EALaunchHelper.exe")) == ("EA App",)
    assert prefix_guard.client_names(("Battle.net.exe", "Agent.exe")) == ("Battle.net",)
    assert prefix_guard.client_names(("upc.exe", "Tool.exe")) == ("Tool.exe", "Ubisoft Connect")


def test_refusal_names_the_client_and_what_to_do(monkeypatch):
    monkeypatch.setattr(prefix_guard, "unwrapped_programs", lambda prefix: ("Battle.net.exe",))
    message = prefix_guard.prefix_refusal("/games/bnet", "Diablo IV")
    assert message.startswith("Battle.net is already running without PenguinBurner. Close it")
    assert "Diablo IV gets the overlay" in message


@pytest.mark.parametrize("programs", [(), None])
def test_nothing_running_or_an_unreadable_probe_never_blocks(monkeypatch, programs):
    monkeypatch.setattr(prefix_guard, "unwrapped_programs", lambda prefix: programs)
    assert prefix_guard.prefix_refusal("/games/prefix", "Game") == ""


def test_probe_failure_is_unknown(monkeypatch):
    monkeypatch.setattr(prefix_guard, "run_on_host", lambda *args, **kwargs: None)
    assert prefix_guard.unwrapped_programs("/games/prefix") is None


def test_lutris_reads_the_wine_runner_prefix(tmp_path):
    from types import SimpleNamespace

    from integrations.lutris.library_source import LutrisLibrarySource

    config = tmp_path / "game.yml"
    config.write_text("game:\n  exe: C:/Battle.net/Battle.net.exe\n  prefix: ~/Games/battlenet\n")
    source = LutrisLibrarySource(manager=object(), home=tmp_path)
    wine = SimpleNamespace(runner="wine", config_path=config)
    assert source.wine_prefix(wine).endswith("/Games/battlenet")
    assert source.wine_prefix(SimpleNamespace(runner="linux", config_path=config)) == ""
    assert source.wine_prefix(SimpleNamespace(runner="wine", config_path=None)) == ""


def test_heroic_reads_the_games_wine_prefix(tmp_path):
    import json
    from types import SimpleNamespace

    from integrations.heroic.library_source import HeroicLibrarySource

    games = tmp_path / ".config/heroic/GamesConfig"
    games.mkdir(parents=True)
    (games.parent / "config.json").write_text("{}")  # how Heroic's tree is recognised
    (games / "Turkey.json").write_text(json.dumps({"Turkey": {"winePrefix": "/games/Turkey"}}))
    source = HeroicLibrarySource(manager=object(), home=tmp_path)
    assert source.wine_prefix(SimpleNamespace(game_id="Turkey", is_native=False)) == "/games/Turkey"
    assert source.wine_prefix(SimpleNamespace(game_id="Turkey", is_native=True)) == ""
    assert source.wine_prefix(SimpleNamespace(game_id="Other", is_native=False)) == ""
