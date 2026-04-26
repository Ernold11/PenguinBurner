from __future__ import annotations

from subprocess_locale import stable_subprocess_env


def test_stable_subprocess_env_forces_c_locale(monkeypatch) -> None:
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    monkeypatch.setenv("LANG", "ja_JP.UTF-8")

    env = stable_subprocess_env()

    assert env["LC_ALL"] == "C"
    assert env["LANG"] == "C"


def test_stable_subprocess_env_preserves_extra_but_keeps_locale() -> None:
    env = stable_subprocess_env(
        {
            "LD_LIBRARY_PATH": "/tmp/compat",
            "LC_ALL": "zh_CN.UTF-8",
            "LANG": "zh_CN.UTF-8",
        }
    )

    assert env["LD_LIBRARY_PATH"] == "/tmp/compat"
    assert env["LC_ALL"] == "C"
    assert env["LANG"] == "C"
