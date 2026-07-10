"""Coverage for ui/profiles.py: profile selection, delete/autostart logic,
status text, and the systemd query wrappers (subprocess monkeypatched).
"""

from __future__ import annotations

from types import SimpleNamespace

import ui.features.profiles.profiles as profiles


_P1 = {"profile_id": "p1", "candidate_id": "c1", "path": "/tmp/p1.json", "display_name": "P1"}
_P2 = {"profile_id": "p2", "candidate_id": "c2", "path": "/tmp/p2.json"}


# --- selection ----------------------------------------------------------------


def test_profile_for_selector_variants() -> None:
    catalog = [_P1, _P2]
    assert profiles.profile_for_selector(catalog, "latest") is _P1
    assert profiles.profile_for_selector(catalog, "p2") is _P2
    assert profiles.profile_for_selector(catalog, "c1") is _P1
    assert profiles.profile_for_selector(catalog, "/tmp/p1.json") is _P1
    assert profiles.profile_for_selector(catalog, "p1") is _P1  # stem/name
    assert profiles.profile_for_selector(catalog, "") is None
    assert profiles.profile_for_selector(catalog, "nope") is None
    assert profiles.profile_for_selector([], "latest") is None


def test_selected_ids_include_selector() -> None:
    assert profiles.selected_profile_ids_include_selector([_P1], ["p1"], "p1") is True
    assert profiles.selected_profile_ids_include_selector([_P1], ["p2"], "p1") is False
    assert profiles.selected_profile_ids_include_selector([_P1], [], "p1") is False


# --- delete / autostart action ------------------------------------------------


def test_delete_autostart_action_non_adaptive() -> None:
    info = {"selector": "p1", "adaptive_auto_uv": False}
    assert profiles.profile_delete_autostart_action([_P1], ["p1"], info) == {
        "action": "remove-systemd"
    }
    assert profiles.profile_delete_autostart_action([_P1, _P2], ["p2"], info) == {
        "action": "keep"
    }
    assert profiles.profile_delete_autostart_action([_P1], [], info) == {"action": "keep"}
    assert profiles.profile_delete_autostart_action([_P1], ["p1"], {"selector": ""}) == {
        "action": "keep"
    }


def test_delete_autostart_action_adaptive_branches(monkeypatch) -> None:
    info = {"selector": "p1", "adaptive_auto_uv": True}
    monkeypatch.setattr(profiles, "resolve_profile_tier_profiles", lambda profs: {"balanced": _P2})

    # Two remaining tiers -> keep.
    monkeypatch.setattr(profiles, "available_adaptive_tiers", lambda resolved: ["a", "b"])
    assert profiles.profile_delete_autostart_action([_P1, _P2], ["p1"], info) == {
        "action": "keep"
    }

    # Exactly one remaining tier -> switch to that profile.
    monkeypatch.setattr(profiles, "available_adaptive_tiers", lambda resolved: ["balanced"])
    assert profiles.profile_delete_autostart_action([_P1, _P2], ["p1"], info) == {
        "action": "switch-profile",
        "profile_id": "p2",
    }

    # No remaining tiers -> remove systemd.
    monkeypatch.setattr(profiles, "available_adaptive_tiers", lambda resolved: [])
    assert profiles.profile_delete_autostart_action([_P1], ["p1"], info)["action"] == (
        "remove-systemd"
    )


# --- capability / label helpers ----------------------------------------------


def test_capability_helpers() -> None:
    assert profiles.profile_can_apply({"final_verified": True}) is True
    assert profiles.profile_can_apply({}) is False
    assert profiles.profile_can_verify(_P1) is True
    assert profiles.profile_can_verify({}) is False
    assert profiles.profile_is_deletable(_P1) is True
    assert profiles.profile_is_deletable({}) is False
    assert profiles.profile_verify_selector(_P1) == "/tmp/p1.json"
    assert profiles.profile_verify_selector({"profile_id": "p9"}) == "p9"


def test_adaptive_tier_keys_and_labels(monkeypatch) -> None:
    monkeypatch.setattr(profiles, "resolve_profile_tier_profiles", lambda profs, **k: {})
    monkeypatch.setattr(profiles, "available_adaptive_tiers", lambda resolved: ["efficiency", "performance"])
    monkeypatch.setattr(profiles, "profile_tier_label", lambda tier: tier.title())
    assert profiles.adaptive_profile_tier_keys([_P1]) == ["efficiency", "performance"]
    assert profiles.adaptive_profile_tier_labels([_P1]) == ["Efficiency", "Performance"]


def test_status_label_and_frequency_voltage() -> None:
    assert profiles.profile_status_label([_P1], "p1") == "P1"  # display_name wins
    # _P2 has no display_name/clock/voltage -> falls back to a non-empty label.
    assert profiles.profile_status_label([_P2], "p2")
    assert profiles.profile_status_label([], "__systemd_default__") == "latest Auto-UV profile"
    assert profiles.profile_status_label([], "ghost") == "ghost"
    assert profiles.profile_frequency_voltage(
        {"lock_clock_mhz": 2500, "candidate_voltage_mv": 900}
    ) == "2500 MHz 900 mV"
    assert profiles.profile_frequency_voltage({"lock_clock_mhz": 2500}) == "2500 MHz"
    assert profiles.profile_frequency_voltage({"candidate_voltage_mv": 900}) == "900 mV"
    assert profiles.profile_frequency_voltage({}) == ""


def test_runner_status_text_branches() -> None:
    catalog = [_P1, _P2]
    running_match = profiles.runner_status_text(
        catalog, running_selector="p1", autostart_selector="p1", running_silent_fan=True
    )
    assert "Currently running profile" in running_match
    assert "Systemd autostart: Yes" in running_match

    running_diff = profiles.runner_status_text(
        catalog, running_selector="p1", autostart_selector="p2"
    )
    assert "Autostart profile" in running_diff and "Systemd autostart: No" in running_diff

    autostart_only = profiles.runner_status_text(catalog, autostart_selector="p2")
    assert "Not running now." in autostart_only

    assert profiles.runner_status_text(catalog) == "No running/autostart profile available yet."


# --- command-text parsing -----------------------------------------------------


def test_profile_info_from_command_text() -> None:
    info = profiles.profile_info_from_command_text(
        "pburn --auto-uv-profile p1 --silent-fan-curve --adaptive-auto-uv"
    )
    assert info == {"selector": "p1", "silent_fan_curve": True, "adaptive_auto_uv": True}
    assert profiles.profile_info_from_command_text("pburn --auto-uv-profile=p2")["selector"] == "p2"
    assert profiles.profile_info_from_command_text("pburn run", default_if_present=True)[
        "selector"
    ] == "__systemd_default__"
    assert profiles.profile_info_from_command_text("")["selector"] == ""


# --- delete confirmation text -------------------------------------------------


def test_delete_confirmation_text_variants() -> None:
    assert "the selected profiles" in profiles.delete_confirmation_text([])
    assert "Auto-UV profile P1" in profiles.delete_confirmation_text(["P1"])
    assert "2 selected profiles" in profiles.delete_confirmation_text(["A", "B"])
    assert "remove the Systemd autostart" in profiles.delete_confirmation_text(
        ["P1"], removes_systemd=True
    )
    assert "last usable Adaptive" in profiles.delete_confirmation_text(
        ["P1"], removes_systemd=True, removes_last_usable_adaptive_profile=True
    )
    assert "last usable Adaptive Auto-UV profiles" in profiles.delete_confirmation_text(
        ["A", "B"], removes_systemd=True, removes_last_usable_adaptive_profile=True
    )
    assert "switch it to" in profiles.delete_confirmation_text(
        ["A", "B"], switches_systemd_to_profile="p9"
    )


# --- systemd wrappers (subprocess + path monkeypatched) -----------------------


def test_systemctl_backed_queries(monkeypatch) -> None:
    monkeypatch.setattr(profiles, "_daemon_status_payload", lambda: {})
    monkeypatch.setattr(
        profiles.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="")
    )
    assert profiles.systemd_service_is_enabled() is True
    assert profiles.penguin_burner_runtime_is_active() is True

    def _boom(*a, **k):
        raise OSError("no systemctl")

    monkeypatch.setattr(profiles.subprocess, "run", _boom)
    assert profiles.systemd_service_is_enabled() is False


def test_legacy_running_exec_start(monkeypatch) -> None:
    monkeypatch.setattr(
        profiles.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="pburn --auto-uv-profile p3\n"),
    )
    assert "--auto-uv-profile p3" in profiles._legacy_systemd_running_exec_start()
    monkeypatch.setattr(
        profiles.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout="x")
    )
    assert profiles._legacy_systemd_running_exec_start() == ""


def test_daemon_unit_autostart_and_entry_exists(monkeypatch, tmp_path) -> None:
    # Boot intent is reported by the daemon; the persistent entry remains the
    # installed unit file.
    unit = tmp_path / "pb.service"
    unit.write_text("[Service]\n", encoding="utf-8")
    legacy = tmp_path / "legacy.service"
    monkeypatch.setattr(profiles, "systemd_service_is_enabled", lambda: True)
    monkeypatch.setattr(
        profiles,
        "boot_runtime_spec",
        lambda **_kwargs: {
            "configured": True,
            "profile_id": "p4",
            "runtime_mode": "static",
            "silent_fan_curve": True,
        },
    )
    monkeypatch.setattr(profiles, "systemd_service_unit_path", lambda: unit)
    monkeypatch.setattr(profiles, "legacy_systemd_service_unit_path", lambda: legacy)
    assert profiles.systemd_autostart_profile_info() == {
        "selector": "p4",
        "silent_fan_curve": True,
        "adaptive_auto_uv": False,
    }
    assert profiles.systemd_unit_entry_exists() is True

    # No unit files -> no persistent entry.
    monkeypatch.setattr(
        profiles, "systemd_service_unit_path", lambda: tmp_path / "missing.service"
    )
    assert profiles.systemd_unit_entry_exists() is False


def test_autostart_and_running_info(monkeypatch) -> None:
    monkeypatch.setattr(profiles, "systemd_service_is_enabled", lambda: False)
    monkeypatch.setattr(
        profiles,
        "_legacy_systemd_autostart_profile_info",
        lambda: {"selector": "", "silent_fan_curve": False, "adaptive_auto_uv": False},
    )
    assert profiles.systemd_autostart_profile_info() == {
        "selector": "",
        "silent_fan_curve": False,
        "adaptive_auto_uv": False,
    }
    monkeypatch.setattr(profiles, "systemd_service_is_enabled", lambda: True)
    monkeypatch.setattr(
        profiles,
        "boot_runtime_spec",
        lambda **_kwargs: {
            "configured": True,
            "profile_id": "p5",
            "runtime_mode": "static",
            "silent_fan_curve": True,
        },
    )
    assert profiles.systemd_autostart_profile_info()["selector"] == "p5"

    monkeypatch.setattr(profiles, "_daemon_status_payload", lambda: {})
    monkeypatch.setattr(profiles, "_legacy_systemd_running_exec_start", lambda: "")
    # Nothing is actually running: the running-profile lookup reports empty and
    # does NOT fall back to the autostart entry (a boot-configured profile is not
    # a running one). Autostart is surfaced separately via
    # systemd_autostart_profile_info().
    assert profiles.running_auto_uv_profile_info()["selector"] == ""


def test_running_info_uses_daemon_status(monkeypatch) -> None:
    monkeypatch.setattr(
        profiles,
        "_daemon_status_payload",
        lambda: {
            "state": "runtime_profile_running",
            "active_job": {
                "type": "runtime_profile",
                "profile_id": "p6",
                "runtime_mode": "adaptive",
                "silent_fan_curve": False,
            },
        },
    )

    assert profiles.penguin_burner_runtime_is_active() is True
    info = profiles.running_auto_uv_profile_info()
    assert info["selector"] == "p6"
    assert info["adaptive_auto_uv"] is True
