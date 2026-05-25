from __future__ import annotations

from pathlib import Path

from cli.runtime_config_file import (
    load_runtime_config,
    persist_q2rtx_source_to_runtime_config,
)
from penguin_burner_errors import NvmlError
from runtime_debug import log
from stability.q2rtx import (
    DEFAULT_DEMO_NAME,
    Q2RTXStabilityConfig,
    StabilityTestError,
    attach_stdout_progress,
    build_long_stability_test_config,
    default_q2rtx_install_data_dir,
    install_latest_q2rtx,
    long_stability_workload_durations,
    print_q2rtx_stability_result,
    resolve_q2rtx_executable,
    run_cuda_stability_test,
    run_q2rtx_stability_test,
)


def build_stability_config(
    args,
    *,
    gpu_index,
    config_path,
    timedemo_loops_override=None,
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

    config, _resolved_config_path = load_runtime_config(config_path)
    config_dir = Path(config_path).expanduser().parent
    default_log_dir = config_dir / "stability-logs"
    stability_config = dict(config.get("stability", {}))
    q2rtx_dir = str(stability_config.get("q2rtx_dir", "")).strip()
    q2rtx_binary = str(stability_config.get("q2rtx_binary", "")).strip()
    cli_q2rtx_dir = str(getattr(args, "stability_q2rtx_dir", "") or "").strip()
    cli_q2rtx_binary = str(getattr(args, "stability_q2rtx_binary", "") or "").strip()
    if cli_q2rtx_dir or cli_q2rtx_binary:
        q2rtx_dir = cli_q2rtx_dir
        q2rtx_binary = cli_q2rtx_binary
    q2rtx_source_from_config = bool(q2rtx_dir or q2rtx_binary)
    should_persist_q2rtx_source = bool(cli_q2rtx_dir or cli_q2rtx_binary)
    emit_dependency_progress(0.0, "Checking Q2RTX dependency setup")

    if q2rtx_dir or q2rtx_binary:
        configured_dir = Path(q2rtx_dir).expanduser() if q2rtx_dir else None
        configured_binary = Path(q2rtx_binary).expanduser() if q2rtx_binary else None
        try:
            resolve_q2rtx_executable(
                q2rtx_dir=configured_dir,
                q2rtx_binary=configured_binary,
            )
        except StabilityTestError as exc:
            if not auto_install_q2rtx:
                print(
                    f"{progress_context}: configured Q2RTX source is not usable: {exc}",
                    flush=True,
                )
            else:
                configured_source = q2rtx_binary or q2rtx_dir
                print(
                    f"{progress_context}: configured Q2RTX source is not usable: "
                    f"{configured_source} ({exc})",
                    flush=True,
                )
                print(
                    f"{progress_context}: ignoring stale Q2RTX source and repairing automatically",
                    flush=True,
                )
                q2rtx_dir = ""
                q2rtx_binary = ""
                should_persist_q2rtx_source = True

    if not q2rtx_dir and not q2rtx_binary:
        q2rtx_dir, should_persist_q2rtx_source = _find_managed_q2rtx_install(
            progress_context,
            q2rtx_source_from_config=q2rtx_source_from_config,
            should_persist_q2rtx_source=should_persist_q2rtx_source,
            emit_dependency_progress=emit_dependency_progress,
        )

    if not q2rtx_dir and not q2rtx_binary:
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
            should_persist_q2rtx_source = True
    if q2rtx_dir or q2rtx_binary:
        configured_source = q2rtx_binary or q2rtx_dir
        print(f"{progress_context}: Q2RTX source {configured_source}", flush=True)
        if should_persist_q2rtx_source:
            try:
                persist_q2rtx_source_to_runtime_config(
                    config_path,
                    q2rtx_dir=q2rtx_dir,
                    q2rtx_binary=q2rtx_binary,
                    progress_context=progress_context,
                )
            except Exception as exc:
                print(
                    f"{progress_context}: warning: failed to save Q2RTX source to config: {exc}",
                    flush=True,
                )

    timedemo_loops = (
        int(timedemo_loops_override) if timedemo_loops_override is not None else None
    )
    if q2rtx_dir or q2rtx_binary:
        emit_dependency_progress(
            100.0,
            "Dependencies are ready",
            source=str(q2rtx_binary or q2rtx_dir),
        )
    return Q2RTXStabilityConfig(
        duration_s=(
            int(duration_override)
            if duration_override is not None
            else int(args.stability_seconds)
        ),
        width=int(args.stability_width),
        height=int(args.stability_height),
        hide_window=not bool(args.show_q2rtx_window),
        demo_name=str(DEFAULT_DEMO_NAME).strip(),
        timedemo_loops=timedemo_loops,
        gpu_index=int(gpu_index),
        q2rtx_dir=Path(q2rtx_dir).expanduser() if q2rtx_dir else None,
        q2rtx_binary=Path(q2rtx_binary).expanduser() if q2rtx_binary else None,
        log_dir=Path(args.stability_log_dir).expanduser()
        if str(args.stability_log_dir).strip()
        else default_log_dir,
    )


def _find_managed_q2rtx_install(
    progress_context,
    *,
    q2rtx_source_from_config,
    should_persist_q2rtx_source,
    emit_dependency_progress,
):
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
        return "", should_persist_q2rtx_source

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
            return (
                str(candidate),
                should_persist_q2rtx_source or not q2rtx_source_from_config,
            )

    if (managed_root / "q2rtx").is_file():
        print(
            f"{progress_context}: found managed Q2RTX install {managed_root}",
            flush=True,
        )
        return (
            str(managed_root),
            should_persist_q2rtx_source or not q2rtx_source_from_config,
        )

    return "", should_persist_q2rtx_source


def build_cuda_stability_config(
    args,
    *,
    gpu_index,
    config_path,
):
    config_dir = Path(config_path).expanduser().parent
    default_log_dir = config_dir / "stability-logs"
    return Q2RTXStabilityConfig(
        duration_s=int(args.stability_seconds),
        width=int(args.stability_width),
        height=int(args.stability_height),
        hide_window=not bool(args.show_q2rtx_window),
        demo_name=str(DEFAULT_DEMO_NAME).strip(),
        timedemo_loops=None,
        gpu_index=int(gpu_index),
        q2rtx_dir=None,
        q2rtx_binary=None,
        log_dir=Path(args.stability_log_dir).expanduser()
        if str(args.stability_log_dir).strip()
        else default_log_dir,
    )


def stability_workload_selection(args) -> tuple[bool, bool]:
    workload = str(getattr(args, "stability_workload", "q2rtx-cuda") or "").strip()
    if workload == "q2rtx":
        return True, False
    if workload == "cuda":
        return False, True
    return True, True


def stability_workload_label(*, include_q2rtx: bool, include_cuda: bool) -> str:
    if include_q2rtx and include_cuda:
        return "Q2RTX timedemo + CUDA compute"
    if include_q2rtx:
        return "Q2RTX timedemo"
    if include_cuda:
        return "CUDA compute"
    return "none"


def stability_workload_split_label(
    total_duration_s: int,
    *,
    include_q2rtx: bool,
    include_cuda: bool,
) -> str:
    q2rtx_duration_s, cuda_duration_s = long_stability_workload_durations(
        int(total_duration_s),
        include_q2rtx=bool(include_q2rtx),
        include_cuda=bool(include_cuda),
    )
    parts = []
    if include_q2rtx:
        parts.append(f"q2rtx={int(q2rtx_duration_s)}s")
    if include_cuda:
        parts.append(f"cuda={int(cuda_duration_s)}s")
    return " ".join(parts)


def run_stability_test(args, *, gpu_index, config_path):
    include_q2rtx, include_cuda = stability_workload_selection(args)
    total_duration_s = int(args.stability_seconds)
    split_label = stability_workload_split_label(
        total_duration_s,
        include_q2rtx=include_q2rtx,
        include_cuda=include_cuda,
    )
    log(f"Stability workload split: {split_label}.")
    stability_config = (
        build_stability_config(
            args,
            gpu_index=gpu_index,
            config_path=config_path,
        )
        if include_q2rtx
        else build_cuda_stability_config(
            args,
            gpu_index=gpu_index,
            config_path=config_path,
        )
    )
    stability_config = build_long_stability_test_config(
        stability_config,
        total_duration_s=total_duration_s,
        include_q2rtx=include_q2rtx,
        include_cuda=include_cuda,
    )
    attach_stdout_progress(stability_config)
    try:
        result = (
            run_q2rtx_stability_test(stability_config)
            if include_q2rtx
            else run_cuda_stability_test(stability_config)
        )
    except StabilityTestError as exc:
        raise NvmlError(f"stability test configuration error: {exc}") from exc

    print_q2rtx_stability_result(result)
    if not result.success:
        raise NvmlError(
            f"stability test failed: {result.reason}; log={result.log_path}"
        )
