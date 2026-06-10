from __future__ import annotations

from penguin_burner_overlay.state import (
    OVERLAY_STATE_ENV,
    OverlayState,
    overlay_state_path,
    read_overlay_state,
    write_overlay_state,
)


def test_overlay_state_path_prefers_explicit_env(tmp_path) -> None:
    path = tmp_path / "state.txt"

    assert overlay_state_path({OVERLAY_STATE_ENV: str(path)}) == path


def test_overlay_state_round_trips_key_value_file(tmp_path) -> None:
    path = tmp_path / "overlay-state.txt"

    write_overlay_state(
        OverlayState(
            gpu_index=0,
            clock_mhz=2760,
            voltage_mv=875,
            profile_tier="Balanced",
            present_fps="58",
            profile_tier_key="balanced",
            profile_id="profile-a",
            adaptive=True,
            updated_unix_ns=123,
        ),
        path=path,
    )

    values = read_overlay_state(path)
    assert values["version"] == "1"
    assert values["updated_unix_ns"] == "123"
    assert values["gpu_index"] == "0"
    assert values["clock_mhz"] == "2760"
    assert values["voltage_mv"] == "875"
    assert values["present_fps"] == "58"
    assert values["profile_tier"] == "Balanced"
    assert values["profile_tier_key"] == "balanced"
    assert values["profile_id"] == "profile-a"
    assert values["adaptive"] == "1"
