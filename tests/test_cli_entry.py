from __future__ import annotations

import cli.entry as entry


def test_dispatch_cli_allows_adaptive_systemd_install_with_two_tiers(monkeypatch) -> None:
    installed = []

    monkeypatch.setattr(entry, "read_auto_uv_profiles", lambda: [{"profile_id": "p1"}])
    monkeypatch.setattr(
        entry,
        "resolve_profile_tier_profiles",
        lambda profiles: {
            "efficiency": {"profile_id": "p1"},
            "performance": {"profile_id": "p2"},
        },
    )
    monkeypatch.setattr(
        entry,
        "available_adaptive_tiers",
        lambda resolved: ["efficiency", "performance"],
    )
    monkeypatch.setattr(
        entry,
        "install_systemd_service",
        lambda program_file, argv, **kwargs: installed.append(
            (program_file, list(argv), kwargs)
        ),
    )

    exit_code = entry.dispatch_cli(
        program_file="/tmp/penguin_burner.py",
        main_callback=lambda *_args, **_kwargs: None,
        argv=["--install-systemd-service", "--adaptive-auto-uv"],
    )

    assert exit_code == 0
    assert installed[0][1] == ["--adaptive-auto-uv"]


def test_dispatch_cli_rejects_adaptive_systemd_install_with_one_tier(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(entry, "read_auto_uv_profiles", lambda: [{"profile_id": "p1"}])
    monkeypatch.setattr(
        entry,
        "resolve_profile_tier_profiles",
        lambda profiles: {"balanced": {"profile_id": "p1"}},
    )
    monkeypatch.setattr(
        entry,
        "available_adaptive_tiers",
        lambda resolved: ["balanced"],
    )
    monkeypatch.setattr(
        entry,
        "install_systemd_service",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("service should not be installed")
        ),
    )

    exit_code = entry.dispatch_cli(
        program_file="/tmp/penguin_burner.py",
        main_callback=lambda *_args, **_kwargs: None,
        argv=["--install-systemd-service", "--adaptive-auto-uv"],
    )

    assert exit_code == 1
    assert "requires at least two saved verified Auto-UV profiles" in capsys.readouterr().err
