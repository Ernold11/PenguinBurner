from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import math
import os
from pathlib import Path
import signal
import shutil
import subprocess
import time

from penguin_burner_paths import claim_desktop_user_ownership
from subprocess_locale import stable_subprocess_env

from .assets import _validate_demo_name, resolve_q2rtx_executable, resolve_workload
from .constants import HIDDEN_WINDOW_POSITION
from .identity import _prepare_q2rtx_subprocess_env, _resolve_q2rtx_run_identity
from .install import _prepare_q2rtx_runtime_env
from .models import (
    Q2RTXStabilityConfig,
    Q2RTXStabilityResult,
    StabilityTestError,
    TelemetrySample,
    TimedemoRun,
)
from .output import (
    _expected_timedemo_frames,
    _extract_timedemo_runs,
    _format_sample_metrics,
    _read_recent_output,
    _scan_output_for_fatal_patterns,
)
from .telemetry import (
    _HiddenNvmlVoltageSession,
    _query_xid_messages_since,
    query_gpu_metrics,
)


def _apply_hidden_window_env(
    env: dict[str, str],
    *,
    hide_window: bool,
    use_headless_gamescope: bool,
) -> dict[str, str]:
    if not hide_window:
        return env
    if use_headless_gamescope:
        return env
    if not env.get("DISPLAY"):
        return env

    hidden_env = dict(env)
    # SDL's offscreen backend cannot create a Vulkan surface for Q2RTX. Use a
    # real X11 Vulkan window, but place it outside the visible desktop.
    hidden_env["SDL_VIDEODRIVER"] = "x11"
    hidden_env["SDL_VIDEO_WINDOW_POS"] = HIDDEN_WINDOW_POSITION
    hidden_env["SDL_VIDEO_X11_FORCE_OVERRIDE_REDIRECT"] = "1"
    hidden_env["SDL_VIDEO_MINIMIZE_ON_FOCUS_LOSS"] = "0"
    return hidden_env


def _headless_gamescope_prefix(config: Q2RTXStabilityConfig) -> list[str]:
    if not config.hide_window:
        return []
    if not config.use_headless_gamescope:
        return []
    gamescope = shutil.which("gamescope")
    if not gamescope:
        return []
    return [
        gamescope,
        "--backend",
        "headless",
        "-W",
        str(int(config.width)),
        "-H",
        str(int(config.height)),
        "-w",
        str(int(config.width)),
        "-h",
        str(int(config.height)),
        "-r",
        "0",
        "--",
    ]


def _wrap_q2rtx_command(
    command: list[str],
    *,
    gamescope_prefix: list[str],
) -> list[str]:
    if not gamescope_prefix:
        return list(command)
    return [*gamescope_prefix, *command]


def _result_looks_like_gamescope_startup_crash(result: Q2RTXStabilityResult) -> bool:
    if bool(result.success):
        return False
    if str(result.reason) not in {
        "timedemo-metrics-missing",
        "timedemo-nonzero-exit",
        "fatal-q2rtx-output",
    }:
        return False
    tail = "\n".join(str(line) for line in result.output_tail)
    if "gamescope" not in tail.lower() and "Gamescope WSI" not in tail:
        return False
    startup_crash_markers = (
        "Segmentation fault",
        "Primary child shut down",
        "failed to read Wayland events",
        "Broken pipe",
    )
    return any(marker in tail for marker in startup_crash_markers)


def _common_q2rtx_args(
    *,
    width: int,
    height: int,
    hide_window: bool,
) -> list[str]:
    geometry = f"{int(width)}x{int(height)}"
    if hide_window:
        geometry = f"{geometry}+{HIDDEN_WINDOW_POSITION.replace(',', '+')}"

    return [
        "+set",
        "sys_console",
        "1",
        "+set",
        "vid_rtx",
        "1",
        "+set",
        "cl_async",
        "1",
        "+set",
        "r_maxfps",
        "0",
        "+set",
        "cl_maxfps",
        "1000",
        "+set",
        "vid_fullscreen",
        "0",
        "+set",
        "vid_geometry",
        geometry,
        "+set",
        "vid_vsync",
        "0",
        "+set",
        "drs_enable",
        "0",
        "+set",
        "drs_minscale",
        "100",
        "+set",
        "drs_maxscale",
        "100",
        "+set",
        "viewsize",
        "100",
        "+set",
        "scr_demobar",
        "0",
        "+set",
        "scr_fps",
        "0",
        "+set",
        "s_enable",
        "0",
        "+set",
        "bloom_enable",
        "1",
        "+set",
        "flt_enable",
        "1",
        "+set",
        "flt_taa",
        "1",
        "+set",
        "flt_fsr_enable",
        "0",
        "+set",
        "gr_enable",
        "1",
        "+set",
        "physical_sky_draw_clouds",
        "1",
        "+set",
        "pt_caustics",
        "1",
        "+set",
        "pt_cameras",
        "1",
        "+set",
        "pt_direct_polygon_lights",
        "1",
        "+set",
        "pt_direct_dyn_lights",
        "1",
        "+set",
        "pt_direct_sun_light",
        "1",
        "+set",
        "pt_indirect_polygon_lights",
        "1",
        "+set",
        "pt_indirect_dyn_lights",
        "1",
        "+set",
        "pt_enable_particles",
        "1",
        "+set",
        "pt_enable_beams",
        "1",
        "+set",
        "pt_enable_sprites",
        "1",
        "+set",
        "pt_enable_surface_lights",
        "1",
        "+set",
        "pt_num_bounce_rays",
        "2",
        "+set",
        "pt_reflect_refract",
        "8",
        "+set",
        "pt_thick_glass",
        "2",
    ]


def build_timedemo_command(
    executable_path: Path,
    *,
    demo_name: str,
    width: int,
    height: int,
    hide_window: bool,
    timedemo_runs: int = 1,
) -> list[str]:
    demo_name = _validate_demo_name(demo_name)
    timedemo_runs = max(1, int(timedemo_runs))
    return [
        str(executable_path),
        *_common_q2rtx_args(
            width=width,
            height=height,
            hide_window=hide_window,
        ),
        "+set",
        "cl_demowait",
        "0",
        "+set",
        "timedemo",
        str(timedemo_runs),
        "+set",
        "nextserver",
        "quit",
        "+demo",
        demo_name,
    ]


def _prepare_log_path(log_dir: Path) -> Path:
    log_dir = log_dir.expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)
    claim_desktop_user_ownership(log_dir, include_parents=True)
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    return log_dir / f"q2rtx-stability-{timestamp}.log"


def _wrap_command_for_live_output(command: list[str]) -> list[str]:
    stdbuf = shutil.which("stdbuf")
    if not stdbuf:
        return list(command)
    return [stdbuf, "-oL", "-eL", *command]


def _child_process_group_preexec(
    child_preexec_fn,
):
    def _preexec() -> None:
        os.setsid()
        if child_preexec_fn is not None:
            child_preexec_fn()

    return _preexec


def _terminate_process_group(
    process: subprocess.Popen | None,
    *,
    timeout_s: float = 5.0,
) -> None:
    if process is None:
        return
    try:
        if process.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=timeout_s)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except ProcessLookupError:
            return
        process.wait(timeout=timeout_s)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def _timedemo_runs_net_seconds(timedemo_runs: list[TimedemoRun]) -> float:
    return sum(max(0.0, float(run.seconds)) for run in timedemo_runs)


def _timedemo_abort_is_immediate(reason: str) -> bool:
    reason = str(reason or "").strip()
    if not reason:
        return False
    if reason == "user-stop-requested":
        return True
    if reason == "fatal-q2rtx-output":
        return True
    if reason.startswith("timedemo-live-stall"):
        return True
    return False


def _managed_q2rtx_process_groups(config: Q2RTXStabilityConfig) -> set[int]:
    try:
        executable_path, _workdir = resolve_q2rtx_executable(
            q2rtx_dir=config.q2rtx_dir,
            q2rtx_binary=config.q2rtx_binary,
        )
    except Exception:
        return set()
    executable_text = str(executable_path)
    result = subprocess.run(
        ["ps", "-eo", "pid=,pgid=,args="],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=stable_subprocess_env(),
        check=False,
    )
    if result.returncode != 0:
        return set()
    current_pid = os.getpid()
    current_pgid = os.getpgid(current_pid)
    process_groups: set[int] = set()
    for line in result.stdout.splitlines():
        parts = line.strip().split(maxsplit=2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            pgid = int(parts[1])
        except ValueError:
            continue
        if pid == current_pid or pgid == current_pgid:
            continue
        args = parts[2]
        if executable_text in args:
            process_groups.add(pgid)
    return process_groups


def cleanup_managed_q2rtx_processes(
    config: Q2RTXStabilityConfig,
    *,
    log=None,
) -> int:
    process_groups = _managed_q2rtx_process_groups(config)
    if not process_groups:
        return 0
    stopped = 0
    for pgid in sorted(process_groups):
        try:
            os.killpg(int(pgid), signal.SIGTERM)
            stopped += 1
        except ProcessLookupError:
            continue
        except Exception as exc:
            if log is not None:
                log(f"Q2RTX cleanup: failed to terminate process group {pgid}: {exc}")
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        live_groups = []
        for pgid in sorted(process_groups):
            try:
                os.killpg(int(pgid), 0)
            except ProcessLookupError:
                continue
            except Exception:
                continue
            live_groups.append(pgid)
        if not live_groups:
            break
        time.sleep(0.1)
    for pgid in sorted(process_groups):
        try:
            os.killpg(int(pgid), 0)
        except ProcessLookupError:
            continue
        except Exception:
            continue
        try:
            os.killpg(int(pgid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception as exc:
            if log is not None:
                log(f"Q2RTX cleanup: failed to kill process group {pgid}: {exc}")
    if log is not None and stopped:
        log(f"Q2RTX cleanup: stopped {stopped} managed process group(s).")
    return stopped


def _run_companion_process(
    *,
    config: Q2RTXStabilityConfig,
    command: tuple[str, ...],
    log_file,
    run_start_monotonic: float,
    cuda_telemetry_samples: list[TelemetrySample],
    voltage_session: _HiddenNvmlVoltageSession,
    section_name: str,
    workload_name: str,
    log_path: Path,
    progress_elapsed_offset_s: float = 0.0,
) -> tuple[int | None, str | None]:
    companion_env = dict(os.environ)
    companion_env["PYTHONUNBUFFERED"] = "1"
    companion_process = subprocess.Popen(
        _wrap_command_for_live_output(list(command)),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        env=companion_env,
        start_new_session=True,
    )
    next_heartbeat_monotonic = time.monotonic()
    next_sample_monotonic = next_heartbeat_monotonic
    companion_start_monotonic = next_heartbeat_monotonic
    companion_exit_code = None
    try:
        while True:
            now_monotonic = time.monotonic()
            wall_elapsed_s = now_monotonic - run_start_monotonic
            pass_elapsed_s = (
                float(progress_elapsed_offset_s)
                + now_monotonic
                - companion_start_monotonic
            )
            companion_exit_code = companion_process.poll()
            latest_sample = None
            if now_monotonic >= next_sample_monotonic:
                latest_sample = query_gpu_metrics(
                    config.gpu_index,
                    voltage_session=voltage_session,
                )
                if latest_sample is not None:
                    latest_sample.elapsed_s = pass_elapsed_s
                    cuda_telemetry_samples.append(latest_sample)
                next_sample_monotonic = now_monotonic + float(config.poll_interval_s)
            if now_monotonic >= next_heartbeat_monotonic:
                latest_metrics = _format_sample_metrics(
                    latest_sample
                    if latest_sample is not None
                    else (
                        cuda_telemetry_samples[-1] if cuda_telemetry_samples else None
                    )
                )
                log_file.write(
                    f"# heartbeat elapsed={wall_elapsed_s:.1f}s "
                    f"net_elapsed={pass_elapsed_s:.1f}s "
                    f"running=cuda"
                    + (f" {latest_metrics}" if latest_metrics else "")
                    + "\n"
                )
                log_file.flush()
                progress_state = {
                    "section_name": section_name,
                    "workload_name": workload_name,
                    "elapsed_s": float(wall_elapsed_s),
                    "net_elapsed_s": float(pass_elapsed_s),
                    "progress_elapsed_s": float(wall_elapsed_s),
                    "command": list(command),
                    "log_path": log_path,
                    "latest_sample": (
                        latest_sample
                        if latest_sample is not None
                        else (
                            cuda_telemetry_samples[-1]
                            if cuda_telemetry_samples
                            else None
                        )
                    ),
                    "telemetry_samples": [],
                    "timedemo_runs": [],
                    "new_timedemo_runs": [],
                    "completed_runs": 0,
                    "completed_frames": 0,
                    "last_run": None,
                    "running": "cuda",
                    "fatal_output_matches": [],
                }
                if config.progress_callback is not None:
                    try:
                        config.progress_callback(progress_state)
                    except Exception:
                        pass
                if config.abort_callback is not None:
                    try:
                        abort_reason = config.abort_callback(progress_state)
                    except Exception:
                        abort_reason = None
                    if abort_reason:
                        _terminate_process_group(companion_process)
                        return companion_process.poll() or -15, str(abort_reason)
                next_heartbeat_monotonic = now_monotonic + 1.0
            if companion_exit_code is not None:
                return companion_exit_code, None
            time.sleep(0.1)
    finally:
        _terminate_process_group(companion_process)


def run_cuda_stability_test(config: Q2RTXStabilityConfig) -> Q2RTXStabilityResult:
    command = tuple(config.companion_command or ())
    if not command:
        raise StabilityTestError("CUDA stability command is not configured")
    if int(config.duration_s) <= 0:
        raise StabilityTestError("CUDA stability duration must be greater than zero")
    if float(config.poll_interval_s) <= 0:
        raise StabilityTestError("stability poll interval must be greater than zero")

    started_at = datetime.now().astimezone()
    log_path = _prepare_log_path(config.log_dir)
    command_text = " ".join(str(part) for part in command)
    log_path.write_text(
        (
            "# PenguinBurner CUDA stability log\n"
            f"# started_at={started_at.isoformat()}\n"
            "# workload=CUDA compute\n"
            f"# command={command_text}\n"
            f"# workdir={Path.cwd()}\n"
        ),
        encoding="utf-8",
    )
    claim_desktop_user_ownership(log_path)

    cuda_telemetry_samples: list[TelemetrySample] = []
    voltage_session = _HiddenNvmlVoltageSession(config.gpu_index)
    run_start_monotonic = time.monotonic()
    exit_code = None
    abort_reason = None
    try:
        with log_path.open("a", encoding="utf-8", errors="replace") as log_file:
            exit_code, abort_reason = _run_companion_process(
                config=config,
                command=command,
                log_file=log_file,
                run_start_monotonic=run_start_monotonic,
                cuda_telemetry_samples=cuda_telemetry_samples,
                voltage_session=voltage_session,
                section_name="cuda-compute",
                workload_name="CUDA compute",
                log_path=log_path,
            )
    finally:
        voltage_session.close()
    observed_duration_s = time.monotonic() - run_start_monotonic
    fatal_output_matches = _scan_output_for_fatal_patterns(log_path)
    xid_messages = _query_xid_messages_since(started_at)
    output_tail = _read_recent_output(log_path)
    if abort_reason:
        reason = str(abort_reason)
        success = False
    elif exit_code not in (None, 0):
        reason = f"cuda-bruteforce-failed exit={int(exit_code)}"
        success = False
    elif fatal_output_matches:
        reason = "fatal-cuda-output"
        success = False
    elif xid_messages:
        reason = "nvidia-xid-detected"
        success = False
    else:
        reason = "ok"
        success = True
    return Q2RTXStabilityResult(
        success=success,
        reason=reason,
        workload_kind="cuda",
        workload_name="CUDA compute",
        command=list(command),
        executable_path=Path(str(command[0])),
        workdir=Path.cwd(),
        duration_requested_s=int(config.duration_s),
        timedemo_loops_requested=None,
        duration_observed_s=float(observed_duration_s),
        demo_path=None,
        log_path=log_path,
        process_exit_code=exit_code,
        shutdown_mode="cuda-complete" if success else reason,
        fatal_output_matches=fatal_output_matches,
        xid_messages=xid_messages,
        timedemo_runs=[],
        telemetry_samples=list(cuda_telemetry_samples),
        companion_telemetry_samples=list(cuda_telemetry_samples),
        output_tail=output_tail,
    )


def _run_timedemo_process(
    *,
    config: Q2RTXStabilityConfig,
    command: list[str],
    workdir: Path,
    log_path: Path,
    section_name: str,
    workload_name: str,
    requested_runs: int | None,
    expected_frames_per_run: int | None,
    runtime_env: dict[str, str],
    initial_run_count: int = 0,
) -> tuple[int | None, float, list[TelemetrySample], list[TelemetrySample], str]:
    telemetry_samples: list[TelemetrySample] = []
    voltage_session = _HiddenNvmlVoltageSession(config.gpu_index)
    gamescope_prefix = _headless_gamescope_prefix(config)
    child_env, child_preexec_fn, child_user_name = _prepare_q2rtx_subprocess_env(
        _apply_hidden_window_env(
            runtime_env,
            hide_window=bool(config.hide_window),
            use_headless_gamescope=bool(gamescope_prefix),
        )
    )
    section_started_dt = datetime.now().astimezone()
    section_started_at = section_started_dt.isoformat()
    run_start_monotonic = time.monotonic()
    next_sample_monotonic = run_start_monotonic
    next_heartbeat_monotonic = run_start_monotonic
    exit_reason = "completed"
    observed_duration_s = 0.0
    last_timedemo_run_count = max(0, int(initial_run_count))
    companion_exit_code = None
    companion_abort_reason = None
    cuda_telemetry_samples: list[TelemetrySample] = []
    process = None

    try:
        with log_path.open("a", encoding="utf-8", errors="replace") as log_file:
            log_file.write(f"\n=== {section_name} start {section_started_at} ===\n")
            if child_user_name is not None:
                log_file.write(f"# q2rtx_run_user={child_user_name}\n")
            if gamescope_prefix:
                log_file.write("# q2rtx_window=gamescope-headless\n")
                log_file.write(
                    "# launch_command="
                    + " ".join(
                        _wrap_q2rtx_command(command, gamescope_prefix=gamescope_prefix)
                    )
                    + "\n"
                )
            elif config.hide_window:
                log_file.write(
                    "# q2rtx_window=offscreen-x11 "
                    f"SDL_VIDEO_WINDOW_POS={HIDDEN_WINDOW_POSITION} "
                    "SDL_VIDEO_X11_FORCE_OVERRIDE_REDIRECT=1\n"
                )
                log_file.write(f"# launch_command={' '.join(command)}\n")
            else:
                log_file.write(f"# launch_command={' '.join(command)}\n")
            if config.companion_command:
                log_file.write(
                    f"# companion_command={' '.join(list(config.companion_command))}\n"
                )
            log_file.flush()
            process = subprocess.Popen(
                _wrap_command_for_live_output(
                    _wrap_q2rtx_command(
                        command,
                        gamescope_prefix=gamescope_prefix,
                    )
                ),
                cwd=workdir,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=child_env,
                preexec_fn=_child_process_group_preexec(child_preexec_fn),
            )

            while True:
                now_monotonic = time.monotonic()
                pass_elapsed_s = now_monotonic - run_start_monotonic
                process_exit_code = process.poll()
                if now_monotonic >= next_heartbeat_monotonic:
                    latest_metrics = _format_sample_metrics(
                        telemetry_samples[-1] if telemetry_samples else None
                    )
                    log_file.write(
                        f"# heartbeat elapsed={pass_elapsed_s:.1f}s "
                        f"running=q2rtx"
                        + (f" {latest_metrics}" if latest_metrics else "")
                        + "\n"
                    )
                    log_file.flush()
                    next_heartbeat_monotonic = now_monotonic + 1.0
                if process_exit_code is not None:
                    observed_duration_s = pass_elapsed_s
                    break

                if pass_elapsed_s >= float(config.single_pass_timeout_s):
                    _terminate_process_group(process)
                    process_exit_code = process.returncode
                    observed_duration_s = time.monotonic() - run_start_monotonic
                    exit_reason = "timedemo-timeout"
                    break

                if now_monotonic >= next_sample_monotonic:
                    sample = query_gpu_metrics(
                        config.gpu_index,
                        voltage_session=voltage_session,
                    )
                    if sample is not None:
                        sample.elapsed_s = pass_elapsed_s
                        telemetry_samples.append(sample)
                    all_timedemo_runs = _extract_timedemo_runs(log_path)
                    timedemo_runs = all_timedemo_runs[max(0, int(initial_run_count)) :]
                    new_timedemo_runs = all_timedemo_runs[last_timedemo_run_count:]
                    last_timedemo_run_count = len(all_timedemo_runs)
                    fatal_output_matches = _scan_output_for_fatal_patterns(log_path)
                    completed_frames = sum(int(run.frames) for run in timedemo_runs)
                    net_elapsed_s = _timedemo_runs_net_seconds(timedemo_runs)
                    progress_state = {
                        "section_name": section_name,
                        "workload_name": workload_name,
                        "started_at": section_started_dt,
                        "elapsed_s": float(pass_elapsed_s),
                        "net_elapsed_s": float(net_elapsed_s),
                        "progress_elapsed_s": float(pass_elapsed_s),
                        "command": list(command),
                        "workdir": workdir,
                        "log_path": log_path,
                        "process_pid": int(process.pid),
                        "expected_runs": (
                            int(requested_runs) if requested_runs is not None else None
                        ),
                        "expected_frames_per_run": (
                            int(expected_frames_per_run)
                            if expected_frames_per_run is not None
                            else None
                        ),
                        "latest_sample": telemetry_samples[-1]
                        if telemetry_samples
                        else None,
                        "telemetry_samples": list(telemetry_samples),
                        "timedemo_runs": list(timedemo_runs),
                        "new_timedemo_runs": list(new_timedemo_runs),
                        "completed_runs": int(len(timedemo_runs)),
                        "completed_frames": int(completed_frames),
                        "last_run": timedemo_runs[-1] if timedemo_runs else None,
                        "running": "q2rtx",
                        "fatal_output_matches": list(fatal_output_matches),
                    }
                    if config.progress_callback is not None:
                        try:
                            config.progress_callback(progress_state)
                        except Exception:
                            pass
                    if config.abort_callback is not None:
                        try:
                            abort_reason = config.abort_callback(progress_state)
                        except Exception:
                            abort_reason = None
                        if abort_reason and _timedemo_abort_is_immediate(
                            str(abort_reason)
                        ):
                            _terminate_process_group(process)
                            process_exit_code = process.returncode
                            observed_duration_s = time.monotonic() - run_start_monotonic
                            exit_reason = str(abort_reason)
                            break
                    next_sample_monotonic = now_monotonic + float(
                        config.poll_interval_s
                    )

                time.sleep(0.1)

            if (
                process_exit_code == 0
                and exit_reason == "completed"
                and config.companion_command is not None
            ):
                q2rtx_net_duration_s = _timedemo_runs_net_seconds(
                    _extract_timedemo_runs(log_path)[max(0, int(initial_run_count)) :]
                )
                companion_exit_code, companion_abort_reason = _run_companion_process(
                    config=config,
                    command=config.companion_command,
                    log_file=log_file,
                    run_start_monotonic=run_start_monotonic,
                    cuda_telemetry_samples=cuda_telemetry_samples,
                    voltage_session=voltage_session,
                    section_name=section_name,
                    workload_name=workload_name,
                    log_path=log_path,
                    progress_elapsed_offset_s=float(q2rtx_net_duration_s),
                )
                if companion_abort_reason:
                    exit_reason = str(companion_abort_reason)
                elif companion_exit_code not in (None, 0):
                    exit_reason = (
                        f"cuda-bruteforce-failed exit={int(companion_exit_code)}"
                    )
    finally:
        _terminate_process_group(process)
        voltage_session.close()

    if companion_abort_reason and exit_reason == "completed":
        exit_reason = str(companion_abort_reason)
    elif companion_exit_code not in (None, 0) and exit_reason == "completed":
        exit_reason = f"cuda-bruteforce-failed exit={int(companion_exit_code)}"

    return (
        process_exit_code,
        observed_duration_s,
        telemetry_samples,
        cuda_telemetry_samples,
        exit_reason,
    )


def _run_timedemo_session(
    *,
    config: Q2RTXStabilityConfig,
    executable_path: Path,
    workdir: Path,
    workload_name: str,
    demo_path: Path | None,
    log_path: Path,
    runtime_env: dict[str, str],
) -> Q2RTXStabilityResult:
    started_at = datetime.now().astimezone()
    telemetry_samples: list[TelemetrySample] = []
    timedemo_runs: list[TimedemoRun] = []
    shutdown_mode = "not-started"
    process_exit_code: int | None = None
    early_failure_reason = ""
    observed_duration_s = 0.0
    command_used: list[str] = []
    companion_telemetry_samples: list[TelemetrySample] = []
    fatal_output_matches: list[str] = []
    xid_messages: list[str] = []
    requested_loops = (
        int(config.timedemo_loops) if config.timedemo_loops is not None else None
    )
    use_headless_gamescope = bool(_headless_gamescope_prefix(config))
    initial_timedemo_runs = requested_loops if requested_loops is not None else 1
    command_used = build_timedemo_command(
        executable_path,
        demo_name=workload_name,
        width=config.width,
        height=config.height,
        hide_window=bool(config.hide_window) and not use_headless_gamescope,
        timedemo_runs=initial_timedemo_runs,
    )
    expected_frames_per_run = _expected_timedemo_frames(workload_name)

    log_path.write_text(
        (
            "# PenguinBurner Q2RTX stability log\n"
            f"# started_at={started_at.isoformat()}\n"
            f"# workload={workload_name}\n"
            f"# command={' '.join(command_used)}\n"
            f"# workdir={workdir}\n"
            f"# demo_name={workload_name}\n"
            f"# demo_path={demo_path if demo_path is not None else '<unresolved>'}\n"
        ),
        encoding="utf-8",
    )
    claim_desktop_user_ownership(log_path)
    run_identity = _resolve_q2rtx_run_identity()
    if run_identity is not None:
        with log_path.open("a", encoding="utf-8", errors="replace") as log_file:
            log_file.write(f"# q2rtx_target_user={run_identity['user_name']}\n")

    if requested_loops is not None:
        (
            process_exit_code,
            observed_duration_s,
            telemetry_samples,
            companion_telemetry_samples,
            shutdown_mode,
        ) = _run_timedemo_process(
            config=config,
            command=command_used,
            workdir=workdir,
            log_path=log_path,
            section_name=f"timedemo-loop x{requested_loops}",
            workload_name=workload_name,
            requested_runs=requested_loops,
            expected_frames_per_run=expected_frames_per_run,
            runtime_env=runtime_env,
        )
        timedemo_runs = _extract_timedemo_runs(log_path)
        if shutdown_mode != "completed":
            early_failure_reason = shutdown_mode
        elif process_exit_code not in (0, None):
            early_failure_reason = "timedemo-nonzero-exit"
        elif len(timedemo_runs) < requested_loops:
            early_failure_reason = "timedemo-metrics-missing"
        else:
            for current_run in timedemo_runs:
                if (
                    current_run.frames <= 0
                    or current_run.seconds <= 0
                    or current_run.fps <= 0
                ):
                    early_failure_reason = "timedemo-metrics-invalid"
                    break

        fatal_output_matches = _scan_output_for_fatal_patterns(log_path)
        xid_messages = _query_xid_messages_since(started_at)
        if not early_failure_reason and fatal_output_matches:
            early_failure_reason = "fatal-q2rtx-output"
        if not early_failure_reason and xid_messages:
            early_failure_reason = "nvidia-xid-detected"
        if not early_failure_reason:
            shutdown_mode = "timedemo-loop-count-complete"
    else:
        calibration_command = command_used
        (
            process_exit_code,
            observed_duration_s,
            calibration_samples,
            companion_telemetry_samples,
            shutdown_mode,
        ) = _run_timedemo_process(
            config=config,
            command=calibration_command,
            workdir=workdir,
            log_path=log_path,
            section_name="timedemo-calibration",
            workload_name=workload_name,
            requested_runs=1,
            expected_frames_per_run=expected_frames_per_run,
            runtime_env=runtime_env,
        )
        calibration_runs = _extract_timedemo_runs(log_path)
        if shutdown_mode != "completed":
            early_failure_reason = shutdown_mode
        elif process_exit_code not in (0, None):
            early_failure_reason = "timedemo-nonzero-exit"
        elif not calibration_runs:
            early_failure_reason = "timedemo-metrics-missing"
        else:
            calibration_run = calibration_runs[-1]
            if (
                calibration_run.frames <= 0
                or calibration_run.seconds <= 0
                or calibration_run.fps <= 0
            ):
                early_failure_reason = "timedemo-metrics-invalid"

        fatal_output_matches = _scan_output_for_fatal_patterns(log_path)
        xid_messages = _query_xid_messages_since(started_at)
        if not early_failure_reason and fatal_output_matches:
            early_failure_reason = "fatal-q2rtx-output"
        if not early_failure_reason and xid_messages:
            early_failure_reason = "nvidia-xid-detected"

        if not early_failure_reason:
            if not calibration_runs:
                raise StabilityTestError("Q2RTX timedemo calibration produced no runs")
            calibration_run = calibration_runs[-1]
            estimated_loop_count = max(
                1,
                int(math.ceil(float(config.duration_s) / calibration_run.seconds)),
            )

            if estimated_loop_count == 1:
                command_used = calibration_command
                telemetry_samples = calibration_samples
                timedemo_runs = calibration_runs
                shutdown_mode = "timedemo-calibration-complete"
            else:
                command_used = build_timedemo_command(
                    executable_path,
                    demo_name=workload_name,
                    width=config.width,
                    height=config.height,
                    hide_window=bool(config.hide_window) and not use_headless_gamescope,
                    timedemo_runs=estimated_loop_count,
                )
                runs_before_loop = len(calibration_runs)
                (
                    process_exit_code,
                    observed_duration_s,
                    telemetry_samples,
                    companion_telemetry_samples,
                    shutdown_mode,
                ) = _run_timedemo_process(
                    config=config,
                    command=command_used,
                    workdir=workdir,
                    log_path=log_path,
                    section_name=f"timedemo-loop x{estimated_loop_count}",
                    workload_name=workload_name,
                    requested_runs=estimated_loop_count,
                    expected_frames_per_run=expected_frames_per_run,
                    runtime_env=runtime_env,
                    initial_run_count=runs_before_loop,
                )
                all_runs = _extract_timedemo_runs(log_path)
                timedemo_runs = all_runs[runs_before_loop:]

                if shutdown_mode != "completed":
                    early_failure_reason = shutdown_mode
                elif process_exit_code not in (0, None):
                    early_failure_reason = "timedemo-nonzero-exit"
                elif len(timedemo_runs) < estimated_loop_count:
                    early_failure_reason = "timedemo-metrics-missing"
                else:
                    for current_run in timedemo_runs:
                        if (
                            current_run.frames <= 0
                            or current_run.seconds <= 0
                            or current_run.fps <= 0
                        ):
                            early_failure_reason = "timedemo-metrics-invalid"
                            break

                fatal_output_matches = _scan_output_for_fatal_patterns(log_path)
                xid_messages = _query_xid_messages_since(started_at)
                if not early_failure_reason and fatal_output_matches:
                    early_failure_reason = "fatal-q2rtx-output"
                if not early_failure_reason and xid_messages:
                    early_failure_reason = "nvidia-xid-detected"

                if not early_failure_reason:
                    shutdown_mode = "timedemo-loop-complete"

    output_tail = _read_recent_output(log_path)

    if early_failure_reason:
        reason = early_failure_reason
        success = False
    elif not timedemo_runs:
        reason = "no-timedemo-runs-completed"
        success = False
    else:
        expected_frames = _expected_timedemo_frames(workload_name)
        frame_counts = {run.frames for run in timedemo_runs}
        if expected_frames is not None and any(
            run.frames != expected_frames for run in timedemo_runs
        ):
            reason = "timedemo-unexpected-frame-count"
            success = False
        elif len(frame_counts) > 1:
            reason = "timedemo-frame-count-drift"
            success = False
        elif fatal_output_matches:
            reason = "fatal-q2rtx-output"
            success = False
        elif xid_messages:
            reason = "nvidia-xid-detected"
            success = False
        else:
            reason = "ok"
            success = True

    return Q2RTXStabilityResult(
        success=success,
        reason=reason,
        workload_kind="timedemo",
        workload_name=workload_name,
        command=command_used,
        executable_path=executable_path,
        workdir=workdir,
        duration_requested_s=int(config.duration_s),
        timedemo_loops_requested=requested_loops,
        duration_observed_s=observed_duration_s,
        demo_path=demo_path,
        log_path=log_path,
        process_exit_code=process_exit_code,
        shutdown_mode=shutdown_mode,
        fatal_output_matches=fatal_output_matches,
        xid_messages=xid_messages,
        timedemo_runs=timedemo_runs,
        telemetry_samples=telemetry_samples,
        companion_telemetry_samples=companion_telemetry_samples,
        output_tail=output_tail,
    )


def run_q2rtx_stability_test(config: Q2RTXStabilityConfig) -> Q2RTXStabilityResult:
    if config.timedemo_loops is None and int(config.duration_s) <= 0:
        raise StabilityTestError("stability duration must be greater than zero")
    if config.timedemo_loops is not None and int(config.timedemo_loops) <= 0:
        raise StabilityTestError("timedemo loop count must be greater than zero")
    if int(config.width) <= 0 or int(config.height) <= 0:
        raise StabilityTestError("stability window size must be positive")
    if float(config.poll_interval_s) <= 0:
        raise StabilityTestError("stability poll interval must be greater than zero")
    if float(config.single_pass_timeout_s) <= 0:
        raise StabilityTestError(
            "single timedemo pass timeout must be greater than zero"
        )

    executable_path, workdir = resolve_q2rtx_executable(
        q2rtx_dir=config.q2rtx_dir,
        q2rtx_binary=config.q2rtx_binary,
    )
    runtime_env, _compat_lib_dir = _prepare_q2rtx_runtime_env(
        executable_path,
        show_progress=False,
    )
    workload_name, demo_path = resolve_workload(
        config.demo_name,
        workdir=workdir,
    )
    log_path = _prepare_log_path(config.log_dir)

    result = _run_timedemo_session(
        config=config,
        executable_path=executable_path,
        workdir=workdir,
        workload_name=workload_name,
        demo_path=demo_path,
        log_path=log_path,
        runtime_env=runtime_env,
    )
    if (
        bool(config.hide_window)
        and bool(config.use_headless_gamescope)
        and _result_looks_like_gamescope_startup_crash(result)
    ):
        print(
            "Q2RTX headless gamescope crashed before timedemo metrics; "
            "retrying with the offscreen X11 fallback.",
            flush=True,
        )
        retry_config = replace(config, use_headless_gamescope=False)
        retry_log_path = _prepare_log_path(config.log_dir)
        retry_result = _run_timedemo_session(
            config=retry_config,
            executable_path=executable_path,
            workdir=workdir,
            workload_name=workload_name,
            demo_path=demo_path,
            log_path=retry_log_path,
            runtime_env=runtime_env,
        )
        if retry_result.success:
            return retry_result
        if result.duration_observed_s <= 0.0:
            return retry_result
    return result
