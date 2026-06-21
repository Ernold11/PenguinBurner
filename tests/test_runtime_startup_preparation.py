from pathlib import Path

from cli.runtime_startup_preparation import (
    RuntimeStartupPreparationDependencies,
    prepare_runtime_startup,
)
from common.penguin_burner_errors import FanCurveBlockedError


def _deps(**overrides):
    defaults = {
        "default_user_config_dir": lambda: Path("/tmp/no-auto-uv-fan-curve"),
        "load_auto_uv_fan_curve": lambda fan_config: None,
        "log": lambda message: None,
    }
    defaults.update(overrides)
    return RuntimeStartupPreparationDependencies(**defaults)


def test_prepare_runtime_startup_loads_auto_uv_fan_curve_when_present(tmp_path):
    logs = []
    (tmp_path / "auto-uv-fan-curve.json").write_text("{}", encoding="utf-8")
    original_fan_config = {"poll_interval_s": 1.0}
    auto_uv_fan_config = {"poll_interval_s": 0.5, "curve_source": "auto-uv"}
    auto_uv_runtime_options = {"auto_uv_mode": "performance"}

    result = prepare_runtime_startup(
        config_path="/tmp/config.json",
        fan_config=original_fan_config,
        gpu_index=0,
        auto_uv_runtime_options=auto_uv_runtime_options,
        fan_control_enabled=True,
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
            log=logs.append,
        ),
    )

    assert result.auto_uv_runtime_options is auto_uv_runtime_options
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
        auto_uv_runtime_options={},
        fan_control_enabled=True,
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


def test_prepare_runtime_startup_keeps_requested_fan_config_without_auto_uv_curve(
    tmp_path,
):
    original_fan_config = {"poll_interval_s": 1.0}

    result = prepare_runtime_startup(
        config_path="/tmp/config.json",
        fan_config=original_fan_config,
        gpu_index=2,
        auto_uv_runtime_options={},
        fan_control_enabled=True,
        auto_uv_final_curve_available=False,
        argv=[],
        journal_hours=24,
        program_file="/tmp/penguin_burner.py",
        interactive=False,
        prompt_yes_no=lambda *args, **kwargs: False,
        dependencies=_deps(default_user_config_dir=lambda: tmp_path),
    )

    assert result.fan_config is original_fan_config
    assert result.fan_control_enabled is True
