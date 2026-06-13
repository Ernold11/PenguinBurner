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
            power_w=220,
            cpu_util_pct=32,
            cpu_peak_thread_pct=98,
            fan_pct=62,
            temperature_c=67,
            uv_offset_mv=-75,
            profile_tier="Balanced",
            present_fps="58",
            framegen_fps="116",
            framegen_active=True,
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
    assert values["power_w"] == "220"
    assert values["cpu_util_pct"] == "32"
    assert values["cpu_peak_thread_pct"] == "98"
    assert values["fan_pct"] == "62"
    assert values["temperature_c"] == "67"
    assert values["uv_offset_mv"] == "-75"
    assert values["present_fps"] == "58"
    assert values["framegen_fps"] == "116"
    assert values["framegen_active"] == "1"
    assert values["profile_tier"] == "Balanced"
    assert values["profile_tier_key"] == "balanced"
    assert values["profile_id"] == "profile-a"
    assert values["adaptive"] == "1"


def test_overlay_state_round_trips_latency_ms(tmp_path) -> None:
    path = tmp_path / "overlay-state.txt"

    write_overlay_state(
        OverlayState(
            gpu_index=0,
            clock_mhz=2760,
            voltage_mv=875,
            profile_tier="Balanced",
            present_fps="58",
            latency_ms="34",
            updated_unix_ns=123,
        ),
        path=path,
    )

    assert read_overlay_state(path)["latency_ms"] == "34"


def test_overlay_state_path_prefers_container_visible_home(tmp_path) -> None:
    path = overlay_state_path(
        {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": "/run/user/1000"}
    )

    assert path == tmp_path / ".cache" / "penguin-burner" / "overlay-state.txt"


def test_overlay_state_publisher_passes_latency_p95_ms(tmp_path, monkeypatch) -> None:
    import runtime_gpu_control.overlay_state_publisher as overlay_state_publisher

    monkeypatch.setattr(
        overlay_state_publisher, "get_power_draw_w", lambda _nvml, _device: 219.6
    )

    class _FailingNvmlSession:
        nvml = None
        device = None

    publisher = overlay_state_publisher.OverlayStatePublisher(
        gpu_index=0,
        nvml_session=_FailingNvmlSession(),
        voltage_reader=None,
        profile_tier="Balanced",
        path=tmp_path / "overlay-state.txt",
        time_ns=lambda: 123,
    )

    written = publisher.publish(
        latency_snapshot={
            "present_fps": "50",
            "raw_present_fps_stats": {"avg": "100"},
            "framegen_active": True,
            "latency_p95_ms": 34.4,
        }
    )

    values = read_overlay_state(written)
    assert values["present_fps"] == "50"
    assert values["framegen_fps"] == "100"
    assert values["framegen_active"] == "1"
    assert values["latency_ms"] == "34"
    assert values["power_w"] == "220"


def test_overlay_state_publisher_writes_fan_temperature_and_uv_offset(
    tmp_path,
    monkeypatch,
) -> None:
    import runtime_gpu_control.overlay_state_publisher as overlay_state_publisher

    monkeypatch.setattr(
        overlay_state_publisher, "get_core_clock_mhz", lambda _nvml, _device: 2500
    )
    monkeypatch.setattr(
        overlay_state_publisher, "get_power_draw_w", lambda _nvml, _device: None
    )
    monkeypatch.setattr(
        overlay_state_publisher,
        "get_reported_fan_speeds",
        lambda _nvml, _device, _fan_count: [60, 64],
    )

    class _NvmlSession:
        nvml = None
        device = object()

        def fan_count(self):
            return 2

        def temperature_c(self):
            return 66.8

    class _VoltageReader:
        def read_microvolts(self, _device):
            return 875000

    class _VfCurveReader:
        def refresh_points(self):
            pass

        def find_nearest_point(self, _clock_mhz, _voltage_uv):
            return {"voltage_uv": 875000}

        def editable_core_points(self):
            return [{"base_freq_khz": 2500000, "voltage_uv": 950000}]

    publisher = overlay_state_publisher.OverlayStatePublisher(
        gpu_index=0,
        nvml_session=_NvmlSession(),
        voltage_reader=_VoltageReader(),
        vf_curve_reader=_VfCurveReader(),
        profile_tier="Balanced",
        path=tmp_path / "overlay-state.txt",
        time_ns=lambda: 123,
    )

    values = read_overlay_state(publisher.publish())

    assert values["fan_pct"] == "62"
    assert values["temperature_c"] == "67"
    assert values["uv_offset_mv"] == "-75"


def test_overlay_state_publisher_writes_cpu_util_from_latency_pid(
    tmp_path,
    monkeypatch,
) -> None:
    import runtime_gpu_control.overlay_state_publisher as overlay_state_publisher

    monkeypatch.setattr(
        overlay_state_publisher,
        "get_gpu_utilization_pct",
        lambda _nvml, _device: 63,
    )

    class _NvmlSession:
        nvml = None
        device = None

        def fan_count(self):
            return 0

        def temperature_c(self):
            return 60

    class _CpuSampler:
        def __init__(self) -> None:
            self.pids = []

        def sample_usage(self, pid):
            self.pids.append(pid)
            return type(
                "CpuUsage",
                (),
                {
                    "process_util_pct": 32,
                    "peak_thread_util_pct": 98,
                },
            )()

    cpu_sampler = _CpuSampler()
    publisher = overlay_state_publisher.OverlayStatePublisher(
        gpu_index=0,
        nvml_session=_NvmlSession(),
        voltage_reader=None,
        profile_tier="Balanced",
        process_cpu_sampler=cpu_sampler,
        path=tmp_path / "overlay-state.txt",
        time_ns=lambda: 123,
    )

    values = read_overlay_state(
        publisher.publish(latency_snapshot={"samples": [{"pid": 1234}]})
    )

    assert cpu_sampler.pids == [1234]
    assert publisher.last_gpu_util_pct == 63
    assert publisher.last_cpu_util_pct == 32
    assert publisher.last_cpu_peak_thread_pct == 98
    assert values["gpu_util_pct"] == "63"
    assert values["cpu_util_pct"] == "32"
    assert values["cpu_peak_thread_pct"] == "98"


def test_overlay_state_publisher_keeps_cpu_util_sticky_through_sample_gap(
    tmp_path,
) -> None:
    import runtime_gpu_control.overlay_state_publisher as overlay_state_publisher

    class _NvmlSession:
        nvml = None
        device = None

        def fan_count(self):
            return 0

        def temperature_c(self):
            return 60

    class _CpuSampler:
        def __init__(self) -> None:
            self.pids = []

        def sample_usage(self, pid):
            self.pids.append(pid)
            return type(
                "CpuUsage",
                (),
                {
                    "process_util_pct": 32,
                    "peak_thread_util_pct": 98,
                },
            )()

    times = iter(
        [
            1_000_000_000,
            2_000_000_000,
            8_000_000_001,
        ]
    )
    cpu_sampler = _CpuSampler()
    publisher = overlay_state_publisher.OverlayStatePublisher(
        gpu_index=0,
        nvml_session=_NvmlSession(),
        voltage_reader=None,
        profile_tier="Balanced",
        update_interval_s=1,
        process_cpu_sampler=cpu_sampler,
        path=tmp_path / "overlay-state.txt",
        time_ns=lambda: next(times),
    )

    first = read_overlay_state(
        publisher.publish(latency_snapshot={"samples": [{"pid": 1234}]})
    )
    second = read_overlay_state(publisher.publish(latency_snapshot=None))
    third = read_overlay_state(publisher.publish(latency_snapshot=None))

    assert cpu_sampler.pids == [1234]
    assert publisher.last_cpu_util_pct is None
    assert publisher.last_cpu_peak_thread_pct is None
    assert first["cpu_util_pct"] == "32"
    assert first["cpu_peak_thread_pct"] == "98"
    assert second["cpu_util_pct"] == "32"
    assert second["cpu_peak_thread_pct"] == "98"
    assert third["cpu_util_pct"] == ""
    assert third["cpu_peak_thread_pct"] == ""


def test_overlay_state_publisher_refreshes_live_overlay_config(tmp_path) -> None:
    import runtime_gpu_control.overlay_state_publisher as overlay_state_publisher
    from penguin_burner_overlay.config import OverlayConfig

    class _NvmlSession:
        nvml = None
        device = None

        def fan_count(self):
            return 0

        def temperature_c(self):
            return 60

    configs = [
        OverlayConfig(enabled=False, update_interval_s=9),
        OverlayConfig(enabled=True, update_interval_s=1),
    ]
    publisher = overlay_state_publisher.OverlayStatePublisher(
        gpu_index=0,
        nvml_session=_NvmlSession(),
        voltage_reader=None,
        profile_tier="Balanced",
        enabled=True,
        update_interval_s=2,
        config_path=tmp_path / "overlay.toml",
        overlay_config_loader=lambda _path: configs.pop(0),
        path=tmp_path / "overlay-state.txt",
        time_ns=lambda: 123,
    )

    publisher.refresh_config()

    assert publisher.enabled is False
    assert publisher.update_interval_s == 9

    publisher.publish()

    assert publisher.enabled is True
    assert publisher.update_interval_s == 1
