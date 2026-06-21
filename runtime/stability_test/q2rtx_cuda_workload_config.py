from __future__ import annotations

from pathlib import Path

from common.penguin_burner_errors import NvmlError
from runtime.support.runtime_debug import log
from auto_uv.stability.q2rtx import (
    DEFAULT_DEMO_NAME,
    Q2RTXStabilityConfig,
    StabilityTestError,
    attach_stdout_progress,
    build_long_stability_test_config,
    default_q2rtx_install_data_dir,
    fetch_latest_q2rtx_release_metadata,
    install_latest_q2rtx,
    long_stability_workload_durations,
    print_q2rtx_stability_result,
    run_q2rtx_stability_test,
)
from auto_uv.stability.q2rtx.resolution import (
    format_q2rtx_resolution_choice,
    resolve_q2rtx_render_resolution,
)


def build_stability_config(
    args,
    *,
    gpu_index,
    config_path,
    duration_override=None,
    auto_install_q2rtx=True,
    progress_context="Q2RTX stability",
    dependency_progress_callback=None,
    dependency_text_progress=True,
):
    def emit_dependency_progress(percent, detail, **payload) -> None:
        if dependency_progress_callback is None:
            return
        data = {
            "label": "Downloading dependencies",
            "percent": round(max(0.0, min(100.0, float(percent))), 1),
            "detail": str(detail),
        }
        data.update(payload)
        dependency_progress_callback(data)

    config_dir = Path(config_path).expanduser().parent
    default_log_dir = config_dir / "stability-logs"
    emit_dependency_progress(0.0, "Checking Q2RTX dependency setup")

    q2rtx_dir = _find_managed_q2rtx_install(
        progress_context,
        emit_dependency_progress=emit_dependency_progress,
    )

    if auto_install_q2rtx:
        q2rtx_dir = _refresh_stale_managed_q2rtx_source(
            progress_context,
            q2rtx_dir=q2rtx_dir,
            dependency_text_progress=dependency_text_progress,
            dependency_progress_callback=dependency_progress_callback,
            emit_dependency_progress=emit_dependency_progress,
        )

    if not q2rtx_dir:
        if not auto_install_q2rtx:
            print(
                f"{progress_context}: no managed Q2RTX install found and auto-install is disabled",
                flush=True,
            )
        else:
            print(
                f"{progress_context}: no managed Q2RTX install found; installing now",
                flush=True,
            )
            emit_dependency_progress(
                4.0,
                "No managed Q2RTX install found; downloading dependencies",
            )
            install_result = install_latest_q2rtx(
                show_progress=bool(dependency_text_progress),
                progress_callback=dependency_progress_callback,
            )
            q2rtx_dir = str(install_result.install_dir)
            print(
                f"{progress_context}: using installed Q2RTX {install_result.version} at {q2rtx_dir}",
                flush=True,
            )

    if q2rtx_dir:
        print(f"{progress_context}: Q2RTX source {q2rtx_dir}", flush=True)
        emit_dependency_progress(
            100.0,
            "Dependencies are ready",
            source=str(q2rtx_dir),
        )
    resolution = resolve_q2rtx_render_resolution(
        gpu_index=int(gpu_index),
        requested_width=None,
        requested_height=None,
    )
    print(
        f"{progress_context}: Q2RTX render resolution "
        f"{format_q2rtx_resolution_choice(resolution)}",
        flush=True,
    )
    return Q2RTXStabilityConfig(
        duration_s=(
            int(duration_override)
            if duration_override is not None
            else int(args.stability_seconds)
        ),
        width=int(resolution.width),
        height=int(resolution.height),
        demo_name=str(DEFAULT_DEMO_NAME).strip(),
        gpu_index=int(gpu_index),
        log_dir=default_log_dir,
    )


def _find_managed_q2rtx_install(
    progress_context,
    *,
    emit_dependency_progress,
) -> str:
    managed_root = default_q2rtx_install_data_dir()
    print(
        f"{progress_context}: checking managed Q2RTX install under {managed_root}",
        flush=True,
    )
    emit_dependency_progress(
        2.0,
        "Checking managed Q2RTX install",
        path=str(managed_root),
    )
    if not managed_root.exists():
        return ""

    version_dirs = sorted(
        (
            path
            for path in managed_root.iterdir()
            if path.is_dir() and path.name != "compat"
        ),
        reverse=True,
    )
    for candidate in version_dirs:
        if (candidate / "q2rtx").is_file() and (candidate / "baseq2").exists():
            print(
                f"{progress_context}: found managed Q2RTX install {candidate}",
                flush=True,
            )
            return str(candidate)

    if (managed_root / "q2rtx").is_file():
        print(
            f"{progress_context}: found managed Q2RTX install {managed_root}",
            flush=True,
        )
        return str(managed_root)

    return ""


def _managed_q2rtx_source_version(source: str) -> str | None:
    source_path = Path(source).expanduser()
    managed_root = default_q2rtx_install_data_dir().expanduser()
    try:
        relative = source_path.resolve().relative_to(managed_root.resolve())
    except (OSError, ValueError):
        return None
    if not relative.parts:
        return ""
    version = str(relative.parts[0]).strip()
    if not version or version == "compat":
        return None
    return version


def _refresh_stale_managed_q2rtx_source(
    progress_context,
    *,
    q2rtx_dir,
    dependency_text_progress,
    dependency_progress_callback,
    emit_dependency_progress,
):
    configured_source = str(q2rtx_dir or "").strip()
    if not configured_source:
        return ""

    current_version = _managed_q2rtx_source_version(configured_source)
    if current_version is None:
        return q2rtx_dir

    try:
        latest_version, _asset_name, _asset_url = fetch_latest_q2rtx_release_metadata()
    except StabilityTestError as exc:
        print(
            f"{progress_context}: warning: could not check latest managed Q2RTX release: {exc}",
            flush=True,
        )
        return q2rtx_dir

    if current_version == latest_version:
        return q2rtx_dir

    print(
        f"{progress_context}: managed Q2RTX {current_version} is older than "
        f"{latest_version}; installing latest release",
        flush=True,
    )
    emit_dependency_progress(
        4.0,
        f"Updating managed Q2RTX to {latest_version}",
        current_version=current_version,
        latest_version=latest_version,
    )
    install_result = install_latest_q2rtx(
        show_progress=bool(dependency_text_progress),
        progress_callback=dependency_progress_callback,
    )
    print(
        f"{progress_context}: using installed Q2RTX {install_result.version} "
        f"at {install_result.install_dir}",
        flush=True,
    )
    return str(install_result.install_dir)


def stability_workload_label() -> str:
    return "Q2RTX benchmark + CUDA compute"


def stability_workload_split_label(total_duration_s: int) -> str:
    q2rtx_duration_s, cuda_duration_s = long_stability_workload_durations(
        int(total_duration_s),
    )
    return f"q2rtx={int(q2rtx_duration_s)}s cuda={int(cuda_duration_s)}s"


def run_stability_test(args, *, gpu_index, config_path):
    total_duration_s = int(args.stability_seconds)
    split_label = stability_workload_split_label(total_duration_s)
    log(f"Stability workload split: {split_label}.")
    stability_config = build_stability_config(
        args,
        gpu_index=gpu_index,
        config_path=config_path,
    )
    stability_config = build_long_stability_test_config(
        stability_config,
        total_duration_s=total_duration_s,
    )
    attach_stdout_progress(stability_config)
    try:
        result = run_q2rtx_stability_test(stability_config)
    except StabilityTestError as exc:
        raise NvmlError(f"stability test configuration error: {exc}") from exc

    print_q2rtx_stability_result(result)
    if not result.success:
        raise NvmlError(
            f"stability test failed: {result.reason}; log={result.log_path}"
        )
