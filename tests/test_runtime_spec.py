from __future__ import annotations

from types import SimpleNamespace

import pytest

from cli.runtime_config_file import default_runtime_config
from runtime import runtime_spec


def _curve(profile_id: str, tier: str = "balanced") -> dict:
    return {
        "path": f"/tmp/auto-uv-profile-{profile_id}.json",
        "profile_id": profile_id,
        "profile_tier": tier.title(),
        "profile_tier_key": tier,
        "plan": [
            {
                "index": 12,
                "voltage_mv": 900,
                "base_mhz": 2700,
                "target_mhz": 2800,
                "new_offset_mhz": 100,
            }
        ],
        "lock_clock_mhz": 2800,
        "candidate_voltage_mv": 900,
        "memory_offset_mhz": 1500,
        "power_limit_w": 320,
        "flatten_target": {
            "source": "auto-uv-final",
            "lock_clock_mhz": 2800,
            "lock_voltage_mv": 900,
            "end_voltage_mv": 900,
            "tail_point_count": 1,
        },
    }


def _stub_runtime_sources(monkeypatch, *, curve=None) -> None:
    config = default_runtime_config()
    config["gpu"]["index"] = 2
    config["gpu"]["enable_persistence_mode"] = False
    monkeypatch.setattr(runtime_spec, "load_runtime_config", lambda: (config, None))
    monkeypatch.setattr(
        runtime_spec,
        "require_daemon_capabilities",
        lambda *required: {"capabilities": list(required)},
    )
    monkeypatch.setattr(
        runtime_spec,
        "gpu_capabilities",
        lambda index: {
            "identity": {
                "uuid": f"GPU-test-{index}",
                "pci_bus_id": "0000:02:00.0",
                "name": "Test GPU",
            }
        },
    )
    monkeypatch.setattr(
        runtime_spec,
        "load_auto_uv_final_curve",
        lambda _selector="": curve,
    )
    monkeypatch.setattr(
        runtime_spec,
        "load_overlay_config",
        lambda: SimpleNamespace(enabled=True, update_interval_s=2),
    )


def test_build_static_runtime_spec_resolves_gpu_and_profile(monkeypatch) -> None:
    _stub_runtime_sources(monkeypatch, curve=_curve("balanced-new"))

    spec = runtime_spec.build_runtime_spec(profile_selector="balanced-new")

    assert spec["mode"] == "static"
    assert spec["gpu"] == {
        "uuid": "GPU-test-2",
        "index_at_resolution": 2,
        "pci_bus_id": "0000:02:00.0",
        "name": "Test GPU",
    }
    assert spec["static_profile"]["profile_id"] == "balanced-new"
    assert spec["static_profile"]["plan"][0]["new_offset_mhz"] == 100
    assert spec["policy"]["enable_persistence_mode"] is False
    assert spec["overlay"] == {"enabled": True, "update_interval_s": 2}


def test_adaptive_runtime_keeps_explicit_old_profile_as_initial_tier(monkeypatch) -> None:
    selected = _curve("balanced-old")
    curves = {
        "balanced-old": selected,
        "balanced-new": _curve("balanced-new"),
        "eff-new": _curve("eff-new", "efficiency"),
    }
    _stub_runtime_sources(monkeypatch, curve=selected)
    monkeypatch.setattr(runtime_spec, "read_auto_uv_profiles", lambda: [{}])
    monkeypatch.setattr(
        runtime_spec,
        "resolve_profile_tier_profiles",
        lambda _profiles: {
            "efficiency": {"profile_id": "eff-new"},
            "balanced": {"profile_id": "balanced-new"},
            "performance": None,
        },
    )
    monkeypatch.setattr(
        runtime_spec,
        "available_adaptive_tiers",
        lambda _resolved: ["efficiency", "balanced"],
    )
    monkeypatch.setattr(
        runtime_spec,
        "load_auto_uv_final_curve",
        lambda selector="": curves.get(selector, selected),
    )

    spec = runtime_spec.build_runtime_spec(
        profile_selector="balanced-old",
        adaptive_auto_uv=True,
    )

    assert spec["mode"] == "adaptive"
    assert spec["adaptive"]["initial_tier"] == "balanced"
    assert spec["adaptive"]["profiles"]["balanced"]["profile_id"] == "balanced-old"
    assert spec["adaptive"]["profiles"]["efficiency"]["profile_id"] == "eff-new"


def test_saved_fan_curve_is_resolved_before_daemon_apply(monkeypatch, tmp_path) -> None:
    path = tmp_path / "auto-uv-fan-curve.json"
    path.write_text(
        """{
          "loaded_temperature_c": 70,
          "fan": {
            "curve": [[45, 0], [60, 30], [75, 60], [80, 75], [90, 100]],
            "poll_interval_s": 1
          }
        }""",
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime_spec, "auto_uv_fan_curve_payload_path", lambda: path)

    fan = runtime_spec._fan_spec(default_runtime_config(), enabled=True)

    assert fan["enabled"] is True
    assert fan["config"]["curve"][0] == [45.0, 0.0]
    assert fan["config"]["curve_source_path"] == str(path)


def test_blocked_fan_curve_becomes_explicit_disabled_notice(monkeypatch, tmp_path) -> None:
    path = tmp_path / "auto-uv-fan-curve.json"
    path.write_text(
        '{"fan_curve_blocked":true,"block_reason":"too-hot"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime_spec, "auto_uv_fan_curve_payload_path", lambda: path)

    fan = runtime_spec._fan_spec(default_runtime_config(), enabled=True)

    assert fan["enabled"] is False
    assert "too-hot" in fan["notice"]


def test_runtime_intent_argv_bridge_is_python_only_and_strict() -> None:
    assert runtime_spec.runtime_intent_from_argv(
        [
            "--auto-uv-profile=profile-a",
            "--silent-fan-curve",
            "--adaptive-auto-uv",
            "--gpu-index",
            "3",
        ]
    ) == {
        "profile_selector": "profile-a",
        "silent_fan_curve": True,
        "adaptive_auto_uv": True,
        "gpu_index": 3,
    }
    with pytest.raises(RuntimeError, match="unsupported runtime profile argument"):
        runtime_spec.runtime_intent_from_argv(["--daemon-api"])
