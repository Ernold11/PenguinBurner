from pathlib import Path

from cli.runtime_startup_preparation import (
    RuntimeStartupPreparationDependencies,
    prepare_runtime_startup,
)
from common.penguin_burner_errors import FanCurveBlockedError


def _base_options(**overrides):
    options = {
        "afterburner_root": "",
        "afterburner_profile": "",
        "afterburner_device_profile": "",
    }
    options.update(overrides)
    return options


def _deps(**overrides):
    defaults = {
        "ensure_afterburner_root_configured": lambda config_path, runtime_options, **kwargs: runtime_options,
        "maybe_handle_first_time_afterburner_setup": lambda **kwargs: False,
        "default_user_config_dir": lambda: Path("/tmp/no-auto-uv-fan-curve"),
        "load_auto_uv_fan_curve": lambda fan_config: None,
        "load_runtime_afterburner_fan_config": lambda fan_config, **kwargs: fan_config,
        "log": lambda message: None,
    }
    defaults.update(overrides)
    return RuntimeStartupPreparationDependencies(**defaults)


def test_prepare_runtime_startup_returns_exit_when_first_time_afterburner_setup_handles_flow():
    maybe_calls = []
    fan_config = {"poll_interval_s": 1.0}

    def ensure(config_path, runtime_options, **kwargs):
        return {
            **runtime_options,
            "afterburner_root": "/afterburner",
            "afterburner_profile": "startup",
            "afterburner_device_profile": "VEN.cfg",
        }

    def maybe(**kwargs):
        maybe_calls.append(kwargs)
        return True

    result = prepare_runtime_startup(
        config_path="/tmp/config.json",
        fan_config=fan_config,
        gpu_index=0,
        afterburner_runtime_options=_base_options(),
        fan_control_enabled=True,
        had_persisted_afterburner_root=False,
        auto_uv_final_curve_available=False,
        argv=["--silent-fan-curve"],
        journal_hours=24,
        program_file="/tmp/penguin_burner.py",
        interactive=True,
        prompt_yes_no=lambda *args, **kwargs: True,
        dependencies=_deps(
            ensure_afterburner_root_configured=ensure,
            maybe_handle_first_time_afterburner_setup=maybe,
            load_runtime_afterburner_fan_config=lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("fan source should not be loaded after handled setup")
            ),
        ),
    )

    assert result.should_exit is True
    assert result.afterburner_root == "/afterburner"
    assert result.afterburner_profile == "startup"
    assert result.afterburner_device_profile == "VEN.cfg"
    assert result.fan_config is fan_config
    assert result.fan_control_enabled is True
    assert maybe_calls[0]["gpu_index"] == 0


def test_prepare_runtime_startup_loads_auto_uv_fan_curve_when_present(tmp_path):
    logs = []
    (tmp_path / "auto-uv-fan-curve.json").write_text("{}", encoding="utf-8")
    original_fan_config = {"poll_interval_s": 1.0}
    auto_uv_fan_config = {"poll_interval_s": 0.5, "curve_source": "auto-uv"}

    result = prepare_runtime_startup(
        config_path="/tmp/config.json",
        fan_config=original_fan_config,
        gpu_index=0,
        afterburner_runtime_options=_base_options(afterburner_root="/afterburner"),
        fan_control_enabled=True,
        had_persisted_afterburner_root=True,
        auto_uv_final_curve_available=True,
        argv=[],
        journal_hours=24,
        program_file="/tmp/penguin_burner.py",
        interactive=False,
        prompt_yes_no=lambda *args, **kwargs: False,
        dependencies=_deps(
            default_user_config_dir=lambda: tmp_path,
            load_auto_uv_fan_curve=lambda fan_config: {
                "fan_config": auto_uv_fan_config
            },
            load_runtime_afterburner_fan_config=lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("Afterburner fan config should not be used")
            ),
            log=logs.append,
        ),
    )

    assert result.fan_config == auto_uv_fan_config
    assert result.fan_control_enabled is True
    assert logs == []


def test_prepare_runtime_startup_disables_manual_fan_control_when_auto_uv_curve_is_blocked(
    tmp_path,
):
    logs = []
    (tmp_path / "auto-uv-fan-curve.json").write_text("{}", encoding="utf-8")
    original_fan_config = {"poll_interval_s": 1.0}

    def blocked(_fan_config):
        raise FanCurveBlockedError("saved curve too hot")

    result = prepare_runtime_startup(
        config_path="/tmp/config.json",
        fan_config=original_fan_config,
        gpu_index=0,
        afterburner_runtime_options=_base_options(afterburner_root="/afterburner"),
        fan_control_enabled=True,
        had_persisted_afterburner_root=True,
        auto_uv_final_curve_available=True,
        argv=[],
        journal_hours=24,
        program_file="/tmp/penguin_burner.py",
        interactive=False,
        prompt_yes_no=lambda *args, **kwargs: False,
        dependencies=_deps(
            default_user_config_dir=lambda: tmp_path,
            load_auto_uv_fan_curve=blocked,
            log=logs.append,
        ),
    )

    assert result.fan_config is original_fan_config
    assert result.fan_control_enabled is False
    assert any("disabled by auto-UV safety guard" in message for message in logs)


def test_prepare_runtime_startup_uses_afterburner_fan_config_when_no_auto_uv_curve(
    tmp_path,
):
    original_fan_config = {"poll_interval_s": 1.0}
    afterburner_fan_config = {"poll_interval_s": 1.0, "curve_source": "afterburner"}
    afterburner_calls = []

    def load_afterburner(fan_config, **kwargs):
        afterburner_calls.append((fan_config, kwargs))
        return afterburner_fan_config

    result = prepare_runtime_startup(
        config_path="/tmp/config.json",
        fan_config=original_fan_config,
        gpu_index=2,
        afterburner_runtime_options=_base_options(afterburner_root="/afterburner"),
        fan_control_enabled=True,
        had_persisted_afterburner_root=True,
        auto_uv_final_curve_available=False,
        argv=[],
        journal_hours=24,
        program_file="/tmp/penguin_burner.py",
        interactive=False,
        prompt_yes_no=lambda *args, **kwargs: False,
        dependencies=_deps(
            default_user_config_dir=lambda: tmp_path,
            load_runtime_afterburner_fan_config=load_afterburner,
        ),
    )

    assert result.fan_config == afterburner_fan_config
    assert result.fan_control_enabled is True
    assert afterburner_calls == [
        (
            original_fan_config,
            {"afterburner_root": "/afterburner", "gpu_index": 2},
        )
    ]
