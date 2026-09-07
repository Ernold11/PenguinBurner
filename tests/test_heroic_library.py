"""Reading the Heroic library out of its per-store caches."""

from __future__ import annotations

import hashlib
import json

from integrations.heroic.library import read_heroic_games
from integrations.heroic.paths import (
    ART_CARD_QUERY,
    game_art_path,
    game_config_path,
    heroic_config_root,
    heroic_installed,
)

ART_URL = "https://cdn.example/art/redacted.jpg"


def _heroic(tmp_path, *, flatpak: bool = False):
    root = (
        tmp_path / ".var/app/com.heroicgameslauncher.hgl/config/heroic"
        if flatpak
        else tmp_path / ".config/heroic"
    )
    (root / "store_cache").mkdir(parents=True)
    (root / "store").mkdir(parents=True)
    (root / "config.json").write_text(json.dumps({"defaultSettings": {}}))
    return root


def _library(root, games, *, name="legendary_library.json", key="library"):
    (root / "store_cache" / name).write_text(json.dumps({key: games}))


def _game(app_name="Turkey", **overrides):
    game = {
        "app_name": app_name,
        "title": "Borderlands",
        "runner": "legendary",
        "is_installed": True,
        "install": {"platform": "Windows", "install_path": "/games/bl"},
        "art_square": ART_URL,
    }
    game.update(overrides)
    return game


def test_a_machine_without_heroic_lists_nothing(tmp_path) -> None:
    """The tab explains itself instead of showing an empty list."""
    assert heroic_installed(tmp_path) is False
    assert read_heroic_games(tmp_path) == ()


def test_every_store_is_read_and_dlc_is_not_a_game(tmp_path) -> None:
    """Heroic lists DLC beside its game but never launches it on its own."""
    root = _heroic(tmp_path)
    _library(root, [_game(), _game("dlc-1", install={"is_dlc": True})])
    _library(root, [_game("1454", runner="gog")], name="gog_library.json", key="games")
    (root / "sideload_apps").mkdir()
    (root / "sideload_apps" / "library.json").write_text(
        json.dumps({"games": [_game("side-1", runner="sideload")]})
    )

    games = read_heroic_games(tmp_path)

    assert {game.game_id for game in games} == {"Turkey", "1454", "side-1"}
    assert {game.runner_label for game in games} == {"Epic", "GOG", "Sideloaded"}


def test_uninstalled_games_have_nothing_to_wrap(tmp_path) -> None:
    root = _heroic(tmp_path)
    _library(root, [_game(is_installed=False)])

    assert read_heroic_games(tmp_path) == ()
    assert len(read_heroic_games(tmp_path, include_uninstalled=True)) == 1


def test_playtime_comes_from_the_session_record_in_hours(tmp_path) -> None:
    """Heroic counts minutes; the merged library compares hours."""
    root = _heroic(tmp_path)
    _library(root, [_game()])
    (root / "store" / "timestamp.json").write_text(
        json.dumps(
            {
                "Turkey": {
                    "lastPlayed": "2025-03-04T16:26:15.569Z",
                    "totalPlayed": 90,
                }
            }
        )
    )

    (game,) = read_heroic_games(tmp_path)

    assert game.playtime_hours == 1.5
    assert game.last_played == 1741105575


def test_an_unplayed_game_reports_nothing_rather_than_guessing(tmp_path) -> None:
    root = _heroic(tmp_path)
    _library(root, [_game()])
    (root / "store" / "timestamp.json").write_text(json.dumps({"Turkey": {}}))

    (game,) = read_heroic_games(tmp_path)

    assert (game.last_played, game.playtime_hours) == (0, 0.0)


def test_a_half_written_cache_leaves_the_tab_empty_not_broken(tmp_path) -> None:
    root = _heroic(tmp_path)
    (root / "store_cache" / "legendary_library.json").write_text("{not json")

    assert read_heroic_games(tmp_path) == ()


def test_art_is_the_image_heroic_already_downloaded(tmp_path) -> None:
    """Nothing is fetched: a library list must not go to the network.

    Epic art is cached under the sized URL the card asked for, GOG art under
    the plain one, so both spellings are tried.
    """
    root = _heroic(tmp_path)
    cache = root / "images-cache"
    cache.mkdir()
    sized = hashlib.sha256(f"{ART_URL}{ART_CARD_QUERY}".encode()).hexdigest()
    (cache / sized).write_bytes(b"\xff\xd8jpeg")

    assert game_art_path("Turkey", (ART_URL,), tmp_path) == cache / sized

    plain = hashlib.sha256(ART_URL.encode()).hexdigest()
    (cache / sized).unlink()
    (cache / plain).write_bytes(b"\xff\xd8jpeg")

    assert game_art_path("Turkey", (ART_URL,), tmp_path) == cache / plain
    assert game_art_path("Turkey", ("https://nothing/here.jpg",), tmp_path) is None


def test_a_downloaded_game_icon_wins_over_the_card_art(tmp_path) -> None:
    root = _heroic(tmp_path)
    (root / "icons").mkdir()
    icon = root / "icons" / "Turkey.jpg"
    icon.write_bytes(b"\xff\xd8jpeg")

    assert game_art_path("Turkey", (ART_URL,), tmp_path) == icon


def test_the_flatpak_tree_is_found_when_the_native_one_is_absent(tmp_path) -> None:
    root = _heroic(tmp_path, flatpak=True)

    assert heroic_config_root(tmp_path) == root
    assert heroic_installed(tmp_path) is True


def test_an_app_name_can_never_escape_the_config_directory(tmp_path) -> None:
    """The name is a store's string spliced into a filename."""
    for unusable in ("", "  ", "..", "../../etc/passwd", "a/b"):
        assert game_config_path(unusable, tmp_path) is None
    assert game_config_path("Turkey", tmp_path) is not None
