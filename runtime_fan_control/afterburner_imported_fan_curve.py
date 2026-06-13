"""Load an imported Afterburner fan curve for runtime fan control.

The function rejects profiles where Afterburner software auto fan control was disabled.
"""

from __future__ import annotations

from pathlib import Path

from afterburner.fan_curve import (
    load_afterburner_fan_settings,
    resolve_afterburner_fan_profile,
)
from afterburner.import_fan_curve import build_imported_fan_section
from common.penguin_burner_errors import NvmlError


def load_runtime_afterburner_fan_config(
    current_fan_config, *, afterburner_root, gpu_index
):
    try:
        settings = load_afterburner_fan_settings(
            resolve_afterburner_fan_profile(afterburner_root=afterburner_root)
        )
    except Exception as exc:
        raise NvmlError(
            f"failed to read the imported Afterburner fan profile under {afterburner_root}: {exc}"
        ) from exc

    settings["afterburner_root"] = Path(afterburner_root).expanduser()
    if not settings["sw_auto_enabled"]:
        raise NvmlError(
            "Afterburner software auto fan control is disabled in the imported profile"
        )

    try:
        return build_imported_fan_section(
            current_fan_config,
            settings,
            gpu_index=gpu_index,
        )
    except SystemExit as exc:
        raise NvmlError(str(exc)) from None
