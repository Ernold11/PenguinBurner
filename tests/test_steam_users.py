from pathlib import Path

from integrations.steam.users import (
    STEAMID64_BASE,
    active_steam_user,
    default_steam_root,
    list_steam_users,
)


JAN_STEAMID64 = STEAMID64_BASE + 78675700
MARZENA_STEAMID64 = STEAMID64_BASE + 1255210572


def _write_steam_home(
    home: Path,
    *,
    jan_most_recent: str = "1",
    marzena_userdata: bool = True,
) -> Path:
    root = home / ".local" / "share" / "Steam"
    (root / "steamapps").mkdir(parents=True)
    (root / "config").mkdir(parents=True)
    (root / "userdata" / "78675700" / "config").mkdir(parents=True)
    if marzena_userdata:
        (root / "userdata" / "1255210572" / "config").mkdir(parents=True)
    (root / "config" / "loginusers.vdf").write_text(
        "\n".join(
            [
                '"users"',
                "{",
                f'\t"{JAN_STEAMID64}"',
                "\t{",
                '\t\t"AccountName"\t\t"jan_pietek"',
                '\t\t"PersonaName"\t\t"jan.pietek"',
                f'\t\t"MostRecent"\t\t"{jan_most_recent}"',
                '\t\t"Timestamp"\t\t"1783368657"',
                "\t}",
                f'\t"{MARZENA_STEAMID64}"',
                "\t{",
                '\t\t"AccountName"\t\t"marzena_badziak"',
                '\t\t"PersonaName"\t\t"marzena.badziak"',
                '\t\t"MostRecent"\t\t"0"',
                '\t\t"Timestamp"\t\t"1779309148"',
                "\t}",
                "}",
            ]
        ),
        encoding="utf-8",
    )
    return root


def test_lists_users_with_account_id_mapping(tmp_path: Path) -> None:
    _write_steam_home(tmp_path)
    users = list_steam_users(tmp_path)
    assert [user.account_id for user in users] == ["78675700", "1255210572"]
    assert users[0].persona_name == "jan.pietek"
    assert users[0].most_recent
    assert users[0].localconfig_path.name == "localconfig.vdf"
    assert "78675700" in str(users[0].localconfig_path)


def test_active_user_is_most_recent(tmp_path: Path) -> None:
    _write_steam_home(tmp_path)
    active = active_steam_user(tmp_path)
    assert active is not None and active.display_name == "jan.pietek"


def test_active_user_falls_back_to_newest_timestamp(tmp_path: Path) -> None:
    _write_steam_home(tmp_path, jan_most_recent="0")
    active = active_steam_user(tmp_path)
    assert active is not None and active.account_id == "78675700"


def test_skips_accounts_without_userdata(tmp_path: Path) -> None:
    _write_steam_home(tmp_path, marzena_userdata=False)
    users = list_steam_users(tmp_path)
    assert [user.account_id for user in users] == ["78675700"]


def test_no_steam_root_yields_nothing(tmp_path: Path) -> None:
    assert list_steam_users(tmp_path) == ()
    assert active_steam_user(tmp_path) is None
    assert default_steam_root(tmp_path) is None
