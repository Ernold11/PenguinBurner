from __future__ import annotations

from pathlib import Path

from integrations.afterburner.import_fan_curve import load_config
from integrations.lact.export import LactExportError
from integrations.lact.export import write_lact_nvidia_config
from common.penguin_burner_paths import default_runtime_config_path

LACT_CONFIG_FILENAME = "config.yaml"


def lact_export_output_path(directory: str | Path) -> Path:
    return Path(directory).expanduser() / LACT_CONFIG_FILENAME


def lact_gpu_id_from_config(path: str | Path) -> str:
    path = Path(path).expanduser()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    in_gpus = False
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not in_gpus:
            if stripped == "gpus:":
                in_gpus = True
            continue
        if not line.startswith((" ", "\t")):
            return ""
        if not line.startswith("  ") or line.startswith("    "):
            continue
        key = stripped.rsplit(":", 1)[0].strip().strip("'\"")
        if key:
            return key
    return ""


def detect_lact_gpu_id(output_dir: str | Path) -> str:
    directory = Path(output_dir).expanduser()
    for config_path in (
        directory / LACT_CONFIG_FILENAME,
        Path("/etc/lact") / LACT_CONFIG_FILENAME,
    ):
        gpu_id = lact_gpu_id_from_config(config_path)
        if gpu_id:
            return gpu_id
    return ""


def write_lact_profile_config(
    profile: dict,
    *,
    output_path: Path,
    gpu_id: str,
    include_fan_curve: bool = False,
) -> tuple[Path, list[str]]:
    profile_path = Path(str(profile.get("path", "")).strip()).expanduser()
    if not profile_path.is_file():
        raise LactExportError("Selected Auto-UV profile file was not found")
    return write_lact_nvidia_config(
        output_path=output_path,
        gpu_id=gpu_id,
        final_curve_path=profile_path,
        include_vf_curve=True,
        include_fan_curve=include_fan_curve,
    )


def current_fan_config() -> dict:
    try:
        config = load_config(default_runtime_config_path())
    except Exception:
        return {}
    fan = config.get("fan", {}) if isinstance(config, dict) else {}
    return dict(fan) if isinstance(fan, dict) else {}
