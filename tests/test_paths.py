from __future__ import annotations

import os
from pathlib import Path
import pwd

from penguin_burner_paths import (
    claim_desktop_user_ownership,
    default_user_config_dir,
    effective_desktop_user_ids,
)


def test_config_dir_uses_penguin_burner_home_override(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PENGUIN_BURNER_HOME", str(tmp_path))

    assert default_user_config_dir() == tmp_path / ".config" / "PenguinBurner"


def test_config_dir_uses_pkexec_desktop_user_when_sudo_user_is_absent(
    monkeypatch,
) -> None:
    user = pwd.getpwuid(os.getuid()).pw_name
    home = Path(pwd.getpwnam(user).pw_dir)
    monkeypatch.delenv("PENGUIN_BURNER_HOME", raising=False)
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setenv("PENGUIN_BURNER_Q2RTX_USER", user)

    assert default_user_config_dir() == home / ".config" / "PenguinBurner"


def test_effective_desktop_user_ids_prefers_pkexec_identity(monkeypatch) -> None:
    monkeypatch.setenv("PENGUIN_BURNER_Q2RTX_UID", "1234")
    monkeypatch.setenv("PENGUIN_BURNER_Q2RTX_GID", "5678")
    monkeypatch.setenv("SUDO_UID", "1")
    monkeypatch.setenv("SUDO_GID", "2")

    assert effective_desktop_user_ids() == (1234, 5678)


def test_effective_desktop_user_ids_ignores_root_target(monkeypatch) -> None:
    monkeypatch.setenv("PENGUIN_BURNER_Q2RTX_UID", "0")
    monkeypatch.setenv("PENGUIN_BURNER_Q2RTX_GID", "0")
    monkeypatch.delenv("PENGUIN_BURNER_Q2RTX_USER", raising=False)
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.delenv("SUDO_UID", raising=False)
    monkeypatch.delenv("SUDO_GID", raising=False)

    assert effective_desktop_user_ids() is None


def test_claim_desktop_user_ownership_allows_runtime_dir(monkeypatch) -> None:
    calls = []
    monkeypatch.setenv("SUDO_USER", "desktop")
    monkeypatch.delenv("SUDO_UID", raising=False)
    monkeypatch.delenv("SUDO_GID", raising=False)
    monkeypatch.setattr("penguin_burner_paths.os.geteuid", lambda: 0)
    monkeypatch.setattr(
        "penguin_burner_paths.pwd.getpwnam",
        lambda user: type(
            "Entry",
            (),
            {"pw_uid": 1234, "pw_gid": 5678, "pw_dir": "/home/desktop"},
        )(),
    )
    monkeypatch.setattr(
        "penguin_burner_paths.os.lchown",
        lambda path, uid, gid: calls.append((Path(path), uid, gid)),
    )

    claim_desktop_user_ownership(Path("/run/user/1234/penguin-burner"))

    assert calls == [(Path("/run/user/1234/penguin-burner"), 1234, 5678)]
