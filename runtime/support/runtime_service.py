#!/usr/bin/env python3

import os
from pathlib import Path
import pwd
import configparser
import shlex
import shutil
import stat
import subprocess
import tempfile
import time

from runtime.daemon_client import DEFAULT_DAEMON_SOCKET
from runtime.daemon_client import apply_runtime_spec
from runtime.daemon_client import clear_boot_runtime_spec
from runtime.daemon_client import daemon_status
from runtime.daemon_client import set_boot_runtime_spec
from runtime.daemon_client import stop_runtime_profile
from runtime.runtime_spec import build_runtime_spec_from_intent, runtime_intent_from_argv
from common.subprocess_locale import stable_subprocess_env


SYSTEMCTL = shutil.which("systemctl") or "systemctl"
LEGACY_PENGUIN_BURNER_UNIT_NAME = "PenguinBurner"
PENGUIN_BURNER_DAEMON_UNIT_NAME = "penguin-burnerd"
PENGUIN_BURNER_UNIT_NAME = PENGUIN_BURNER_DAEMON_UNIT_NAME
PENGUIN_BURNER_FOREGROUND_ENV = "PENGUIN_BURNER_FOREGROUND"
ALLOWED_UID_ENV = "PENGUIN_BURNER_DAEMON_ALLOWED_UID"
AUTOSTART_PROGRAM_FILE_ENV = "PENGUIN_BURNER_DAEMON_PROGRAM_FILE"

# Legacy cleanup path from pre-RuntimeSpec releases.
LAST_RUNTIME_STATE_PATH = Path("/var/lib/penguin-burner/last-runtime.json")
BOOT_RUNTIME_STATE_PATH = Path("/var/lib/penguin-burner/boot-runtime.json")
DEFAULT_JOURNAL_HOURS = 4
FLATPAK_ID_ENV = "FLATPAK_ID"
FLATPAK_INFO_PATH = Path("/.flatpak-info")
DESKTOP_RUNTIME_ENV_NAMES = (
    "PENGUIN_BURNER_HOME",
    "PENGUIN_BURNER_Q2RTX_USER",
    "PENGUIN_BURNER_Q2RTX_UID",
    "PENGUIN_BURNER_Q2RTX_GID",
)
# The only executable path permitted in the root systemd unit. Wheel/dev copies
# are installation sources, never persistent root execution paths.
LIBEXEC_DAEMON_BINARY = Path("/usr/libexec/penguin-burnerd")


def parse_runtime_flags(argv, *, default_journal_hours=DEFAULT_JOURNAL_HOURS):
    daemonize = False
    install_systemd_service = False
    uninstall_systemd_service = False
    reset_gpu_defaults = False
    migrate_to_daemon = False
    daemon_status_requested = False
    journal_hours = default_journal_hours
    passthrough = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--daemonize":
            daemonize = True
            index += 1
            continue
        if arg == "--install-systemd-service":
            install_systemd_service = True
            index += 1
            continue
        if arg in ("--uninstall-systemd-service", "--deinstall-systemd-service"):
            uninstall_systemd_service = True
            index += 1
            continue
        if arg == "--reset-gpu-defaults":
            reset_gpu_defaults = True
            index += 1
            continue
        if arg == "--migrate-to-daemon-service":
            migrate_to_daemon = True
            index += 1
            continue
        if arg == "--daemon-status":
            daemon_status_requested = True
            index += 1
            continue
        if arg == "--journal-hours":
            if index + 1 >= len(argv):
                raise RuntimeError("--journal-hours requires a value")
            index += 1
            arg = f"--journal-hours={argv[index]}"
        if arg.startswith("--journal-hours="):
            value = arg.split("=", 1)[1].strip()
            if not value:
                raise RuntimeError("--journal-hours requires a value")
            journal_hours = max(1, int(float(value)))
            index += 1
            continue
        passthrough.append(arg)
        index += 1
    if install_systemd_service and uninstall_systemd_service:
        raise RuntimeError(
            "choose either --install-systemd-service or --uninstall-systemd-service"
        )
    return {
        "daemonize": daemonize,
        "install_systemd_service": install_systemd_service,
        "uninstall_systemd_service": uninstall_systemd_service,
        "reset_gpu_defaults": reset_gpu_defaults,
        "migrate_to_daemon": migrate_to_daemon,
        "daemon_status": daemon_status_requested,
        "journal_hours": journal_hours,
        "passthrough": passthrough,
    }


def running_under_systemd_service():
    return os.environ.get(PENGUIN_BURNER_FOREGROUND_ENV) == "1"


def systemd_is_available():
    return (
        Path("/run/systemd/system").exists() and shutil.which("systemctl") is not None
    )


def journalctl_follow_command(hours):
    return f'journalctl -u {PENGUIN_BURNER_UNIT_NAME}.service --since "-{int(hours)} hours" -f'


def systemd_service_unit_path():
    return daemon_systemd_service_unit_path()


def legacy_systemd_service_unit_path():
    return Path("/etc/systemd/system") / f"{LEGACY_PENGUIN_BURNER_UNIT_NAME}.service"


def daemon_systemd_service_unit_path():
    return Path("/etc/systemd/system") / f"{PENGUIN_BURNER_DAEMON_UNIT_NAME}.service"


def _invoking_user_name():
    for env_name in ("SUDO_USER", "PENGUIN_BURNER_Q2RTX_USER"):
        user = os.environ.get(env_name, "").strip()
        if user:
            return user
    return pwd.getpwuid(os.getuid()).pw_name


def desktop_runtime_env_assignments() -> list[str]:
    assignments = [f"SUDO_USER={_invoking_user_name()}"]
    for env_name in DESKTOP_RUNTIME_ENV_NAMES:
        value = os.environ.get(env_name, "").strip()
        if value:
            assignments.append(f"{env_name}={value}")
    if running_in_flatpak():
        assignments.extend(_flatpak_host_user_env_assignments())
    return assignments


def running_in_flatpak() -> bool:
    return bool(os.environ.get(FLATPAK_ID_ENV, "").strip()) or Path(
        "/.flatpak-info"
    ).is_file()


def flatpak_host_app_path() -> Path:
    override = os.environ.get("PENGUIN_BURNER_FLATPAK_APP_PATH", "").strip()
    if override:
        return _stable_flatpak_deployment_path(Path(override).expanduser())
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(FLATPAK_INFO_PATH, encoding="utf-8")
    except OSError:
        return Path("/app")
    app_path = parser.get("Instance", "app-path", fallback="").strip()
    return _stable_flatpak_deployment_path(Path(app_path)) if app_path else Path("/app")


def _stable_flatpak_deployment_path(path: str | Path) -> Path:
    item = Path(path).expanduser()
    parts = item.parts
    for index, part in enumerate(parts):
        if part != "flatpak":
            continue
        if index + 6 > len(parts):
            continue
        if parts[index + 1] != "app":
            continue
        deploy_index = index + 5
        deployment = parts[deploy_index]
        if deployment == "active":
            return item
        if _looks_like_flatpak_deployment_id(deployment):
            return Path(*parts[:deploy_index], "active", *parts[deploy_index + 1 :])
    return item


def _looks_like_flatpak_deployment_id(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdefABCDEF" for char in value)


def _flatpak_host_path_for_app_path(path: str | Path) -> Path:
    item = Path(str(path))
    if item.is_absolute() and len(item.parts) >= 2 and item.parts[1] == "app":
        return _stable_flatpak_deployment_path(
            flatpak_host_app_path().joinpath(*item.parts[2:])
        )
    return _stable_flatpak_deployment_path(item)


def flatpak_host_site_packages_path(program_file: str | Path | None = None) -> Path:
    override = os.environ.get("PENGUIN_BURNER_FLATPAK_SITE_PACKAGES", "").strip()
    if override:
        return _stable_flatpak_deployment_path(Path(override).expanduser())
    if program_file is not None:
        mapped_program = _flatpak_host_path_for_app_path(program_file)
        if mapped_program.parent.name == "site-packages":
            return mapped_program.parent
    app_path = flatpak_host_app_path()
    candidates = sorted((app_path / "lib").glob("python*/site-packages"), reverse=True)
    if candidates:
        return candidates[0]
    local_relative = _flatpak_local_site_packages_relative_path()
    if local_relative is not None:
        return app_path / local_relative
    if program_file is not None:
        mapped_program = _flatpak_host_path_for_app_path(program_file)
        if mapped_program.name == "penguin_burner.py":
            return mapped_program.parent
    raise RuntimeError(f"could not locate Flatpak Python site-packages under {app_path}")


def _flatpak_local_site_packages_relative_path() -> Path | None:
    candidates = sorted(Path("/app/lib").glob("python*/site-packages"), reverse=True)
    if not candidates:
        return None
    try:
        return candidates[0].relative_to("/app")
    except ValueError:
        return None


def flatpak_host_cli_program_file(program_file: str | Path | None = None) -> Path:
    return flatpak_host_site_packages_path(program_file) / "penguin_burner.py"


def _desktop_user_home() -> str:
    override = os.environ.get("PENGUIN_BURNER_HOME", "").strip()
    if override:
        return str(Path(override).expanduser())
    user = _invoking_user_name()
    if user:
        try:
            return pwd.getpwnam(user).pw_dir
        except KeyError:
            pass
    uid = (
        os.environ.get("PENGUIN_BURNER_Q2RTX_UID", "").strip()
        or os.environ.get("SUDO_UID", "").strip()
    )
    if uid:
        try:
            return pwd.getpwuid(int(uid)).pw_dir
        except (KeyError, ValueError):
            pass
    home = str(Path.home())
    return home if home and home != "/" else ""


def _flatpak_host_user_env_assignments() -> list[str]:
    home = _desktop_user_home()
    if not home:
        return []
    return [
        f"HOME={home}",
        f"XDG_DATA_HOME={home}/.local/share",
    ]


def daemon_allowed_uid_assignment() -> str:
    uid = (
        os.environ.get("PENGUIN_BURNER_Q2RTX_UID", "").strip()
        or os.environ.get("SUDO_UID", "").strip()
    )
    if not uid and os.getuid() != 0:
        uid = str(os.getuid())
    return f"{ALLOWED_UID_ENV}={uid}" if uid else ""


def _format_systemd_exec(args):
    rendered = []
    for arg in args:
        text = str(arg).replace("%", "%%")
        rendered.append(shlex.quote(text))
    return " ".join(rendered)


def runtime_python_env_assignments(program_file) -> list[str]:
    if not running_in_flatpak():
        return []
    return [
        "PYTHONNOUSERSITE=1",
        "PYTHONDONTWRITEBYTECODE=1",
        f"PYTHONPATH={flatpak_host_site_packages_path(program_file)}",
    ]


def run_checked_subprocess(args):
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=stable_subprocess_env(),
        check=False,
    )
    if result.returncode != 0:
        output = (result.stdout.strip() or result.stderr.strip()).strip()
        command_text = " ".join(shlex.quote(str(arg)) for arg in args)
        raise RuntimeError(f"{command_text} failed: {output or result.returncode}")
    return result


def _packaged_daemon_binary() -> Path:
    """penguin-burnerd bundled inside the installed wheel / site-packages.

    setup.py's build_py cargo-builds the daemon and stages it at
    ``runtime/daemon_bin/penguin-burnerd`` (package data). This module lives at
    ``runtime/support/runtime_service.py``, so the sibling ``daemon_bin`` dir is
    one level up from ``support``. This is the *source* copy the elevated install
    step reads from to populate the root-owned /usr/libexec target.
    """
    return Path(__file__).resolve().parent.parent / "daemon_bin" / "penguin-burnerd"


def _dev_daemon_binary(program_file) -> Path:
    """Cargo release build sitting next to a dev checkout's sources."""
    return (
        Path(program_file).resolve().parent
        / "burnerd"
        / "target"
        / "release"
        / "penguin-burnerd"
    )


def daemon_binary_path(program_file, *, binary_path=None) -> str:
    """Return the sole root-service executable path.

    ``binary_path`` survives only for the Flatpak unit builder; it must name the
    same fixed host path. A unit can never point into site-packages or a checkout.
    """
    del program_file
    if binary_path is not None and Path(binary_path) != LIBEXEC_DAEMON_BINARY:
        raise RuntimeError(
            "penguin-burnerd service binary must be installed at "
            f"{LIBEXEC_DAEMON_BINARY}, not {binary_path}"
        )
    return str(LIBEXEC_DAEMON_BINARY)


def daemon_install_source_path(program_file, *, source_path=None) -> Path:
    """Select bytes for the privileged copy into ``/usr/libexec``.

    Prefer the current wheel/dev payload over an older installed daemon so a
    repair also performs an update. A safe existing libexec binary is the final
    fallback for distro packages that do not bundle a second copy.
    """
    candidates = (
        [Path(source_path)]
        if source_path is not None
        else [
            _packaged_daemon_binary(),
            _dev_daemon_binary(program_file),
            LIBEXEC_DAEMON_BINARY,
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    searched = ", ".join(str(candidate) for candidate in candidates)
    raise RuntimeError(
        "penguin-burnerd install source not found (looked in: "
        f"{searched}). Install a daemon-bearing package or build it with "
        "`cargo build --release` in burnerd/."
    )


def _daemon_metadata_is_safe(metadata, *, owner_uid=0) -> bool:
    mode = metadata.st_mode
    return (
        stat.S_ISREG(mode)
        and metadata.st_uid == owner_uid
        and mode & 0o100 != 0
        and mode & 0o022 == 0
    )


def _installed_daemon_is_safe(path: Path, *, owner_uid=0) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return _daemon_metadata_is_safe(metadata, owner_uid=owner_uid)


def _read_daemon_install_source(source: Path) -> bytes:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise RuntimeError(
            f"cannot safely open daemon install source {source}: {exc}"
        ) from exc
    payload = b""
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError(f"daemon install source is not a regular file: {source}")
        with os.fdopen(descriptor, "rb") as source_file:
            descriptor = -1
            payload = source_file.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not payload.startswith(b"\x7fELF"):
        raise RuntimeError(f"daemon install source is not an ELF binary: {source}")
    return payload


def _atomic_install_daemon_binary(
    source: Path,
    destination: Path,
    *,
    owner_uid=0,
    owner_gid=0,
) -> bool:
    """Install ``source`` atomically; return whether destination changed."""
    source = Path(source)
    destination = Path(destination)
    payload = _read_daemon_install_source(source)
    parent = destination.parent
    try:
        parent.mkdir(parents=True, mode=0o755, exist_ok=True)
        parent_metadata = parent.lstat()
    except OSError as exc:
        raise RuntimeError(f"cannot create daemon install directory {parent}: {exc}") from exc
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_uid != owner_uid
        or parent_metadata.st_mode & 0o022
    ):
        raise RuntimeError(f"daemon install directory is not safely owned: {parent}")

    temporary_fd = -1
    temporary_path = None
    try:
        if source == destination:
            if not _installed_daemon_is_safe(
                destination, owner_uid=owner_uid
            ):
                raise RuntimeError(
                    f"installed daemon is not a safe ELF executable: {destination}"
                )
            return False

        if _installed_daemon_is_safe(
            destination, owner_uid=owner_uid
        ) and destination.read_bytes() == payload:
            return False

        temporary_fd, temp_path = tempfile.mkstemp(
            prefix=".penguin-burnerd.", dir=parent
        )
        temporary_path = Path(temp_path)
        with os.fdopen(temporary_fd, "wb") as temporary_file:
            temporary_fd = -1
            temporary_file.write(payload)
            temporary_file.flush()
            os.fchown(temporary_file.fileno(), owner_uid, owner_gid)
            os.fchmod(temporary_file.fileno(), 0o755)
            os.fsync(temporary_file.fileno())

        os.replace(temporary_path, destination)
        temporary_path = None
        return True
    except OSError as exc:
        raise RuntimeError(
            f"could not atomically install {source} at {destination}: {exc}"
        ) from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass
        if temporary_fd >= 0:
            try:
                os.close(temporary_fd)
            except OSError:
                pass


def install_daemon_binary(program_file, *, source_path=None) -> bool:
    if os.geteuid() != 0:
        raise RuntimeError("installing penguin-burnerd requires root privileges")
    source = daemon_install_source_path(program_file, source_path=source_path)
    return _atomic_install_daemon_binary(source, LIBEXEC_DAEMON_BINARY)


def _daemon_program_file_for_unit(program_file) -> Path:
    """The Python CLI the daemon re-launches for Auto-UV scan children.

    Flatpak maps it to the host deployment path; otherwise it is the resolved
    program file. This is what the unit's PENGUIN_BURNER_DAEMON_PROGRAM_FILE env
    and daemon-spawned scan children use.
    """
    if running_in_flatpak():
        return flatpak_host_cli_program_file(program_file)
    return Path(program_file).resolve()


def clear_last_runtime_state() -> None:
    """Clear boot state during the privileged service lifecycle."""
    for path in (LAST_RUNTIME_STATE_PATH, BOOT_RUNTIME_STATE_PATH):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _stop_active_runtime_before_daemon_restart() -> None:
    try:
        stop_runtime_profile(socket_path=DEFAULT_DAEMON_SOCKET, timeout_s=3)
    except Exception:
        pass


def _apply_runtime(argv, *, socket_path=DEFAULT_DAEMON_SOCKET) -> tuple[dict, dict]:
    spec = build_runtime_spec_from_intent(
        runtime_intent_from_argv(argv),
        socket_path=socket_path,
    )
    result = apply_runtime_spec(spec, socket_path=socket_path, timeout_s=45)
    return result, spec


def _apply_persistent_runtime(argv, *, socket_path=DEFAULT_DAEMON_SOCKET) -> dict:
    result, spec = _apply_runtime(argv, socket_path=socket_path)
    set_boot_runtime_spec(spec, socket_path=socket_path, timeout_s=10)
    return result


def build_daemon_api_service_unit(
    program_file,
    *,
    socket_path=DEFAULT_DAEMON_SOCKET,
    binary_path=None,
) -> str:
    allowed_uid = daemon_allowed_uid_assignment()
    allowed_uid_env = f"Environment={allowed_uid}\n" if allowed_uid else ""
    runtime_env = "".join(
        f"Environment={assignment}\n"
        for assignment in [
            *desktop_runtime_env_assignments(),
            *runtime_python_env_assignments(program_file),
        ]
    )

    # The compiled Rust penguin-burnerd binary. Boot intent is stored separately
    # through the typed daemon API. Type=notify + WatchdogSec: the daemon sends
    # READY=1 and heartbeats WATCHDOG=1. Flatpak callers may pass
    # binary_path=/usr/libexec/penguin-burnerd explicitly, but every unit is
    # constrained to that same root-owned path.
    binary = daemon_binary_path(program_file, binary_path=binary_path)
    exec_start = _format_systemd_exec([binary, "--socket", str(socket_path)])
    program_file_env = (
        f"Environment={AUTOSTART_PROGRAM_FILE_ENV}="
        f"{_daemon_program_file_for_unit(program_file)}\n"
    )
    return (
        "[Unit]\n"
        "Description=PenguinBurner hardware daemon\n"
        "After=multi-user.target\n"
        "\n"
        "[Service]\n"
        "Type=notify\n"
        "WorkingDirectory=/\n"
        "WatchdogSec=30\n"
        f"{runtime_env}"
        f"{allowed_uid_env}"
        f"{program_file_env}"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n"
        "RestartSec=2\n"
        "StandardOutput=journal\n"
        "StandardError=journal\n"
        f"SyslogIdentifier={PENGUIN_BURNER_DAEMON_UNIT_NAME}\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def install_systemd_service(program_file, argv, *, journal_hours, log):
    if not systemd_is_available():
        raise RuntimeError("systemd service install is unavailable on this system.")
    if os.geteuid() != 0:
        raise RuntimeError(
            "systemd service install requires root privileges. Re-run with sudo."
        )

    install_daemon_binary(program_file)
    _stop_active_runtime_before_daemon_restart()
    clear_last_runtime_state()
    clear_existing_penguin_burner_unit_for_install(log=log)
    unit_path = daemon_systemd_service_unit_path()
    unit_path.write_text(
        build_daemon_api_service_unit(program_file),
        encoding="utf-8",
    )
    run_checked_subprocess([SYSTEMCTL, "daemon-reload"])
    subprocess.run(
        [SYSTEMCTL, "reset-failed", unit_path.name],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=stable_subprocess_env(),
        check=False,
    )
    _enable_and_start_or_restart_daemon_unit(unit_path.name)
    _wait_for_daemon_status(DEFAULT_DAEMON_SOCKET)
    if argv:
        _apply_persistent_runtime(argv)
    else:
        clear_boot_runtime_spec(socket_path=DEFAULT_DAEMON_SOCKET)
    log(f"Installed and enabled {unit_path.name} at {unit_path}.")
    log(f"Follow the journal with: {journalctl_follow_command(journal_hours)}")


def migrate_to_daemon_service(program_file, *, socket_path=DEFAULT_DAEMON_SOCKET, log):
    if not systemd_is_available():
        raise RuntimeError(
            "PenguinBurner daemon service install is unavailable on this system."
        )
    if os.geteuid() != 0:
        raise RuntimeError(
            "PenguinBurner daemon service migration requires root privileges. "
            "Re-run with sudo."
        )

    legacy_state = read_legacy_service_state()
    raw_argv = (
        legacy_state["runtime_argv"]
        if legacy_state["exists"] and legacy_state["enabled"]
        else []
    )
    autostart_argv = [str(arg) for arg in raw_argv] if isinstance(raw_argv, list) else []
    if legacy_state["exists"] and legacy_state["enabled"] and not autostart_argv:
        raise RuntimeError(
            "existing enabled PenguinBurner.service could not be parsed; "
            "leaving the legacy service unchanged"
        )
    install_daemon_binary(program_file)
    _stop_active_runtime_before_daemon_restart()
    clear_last_runtime_state()
    unit_path = daemon_systemd_service_unit_path()
    unit_path.write_text(
        build_daemon_api_service_unit(program_file, socket_path=socket_path),
        encoding="utf-8",
    )
    run_checked_subprocess([SYSTEMCTL, "daemon-reload"])
    subprocess.run(
        [SYSTEMCTL, "reset-failed", unit_path.name],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=stable_subprocess_env(),
        check=False,
    )
    _enable_and_start_or_restart_daemon_unit(unit_path.name)
    _wait_for_daemon_status(socket_path)
    if autostart_argv:
        _apply_persistent_runtime(autostart_argv, socket_path=socket_path)
    else:
        clear_boot_runtime_spec(socket_path=socket_path)
    log(f"Installed and started {unit_path.name} at {unit_path}.")

    if legacy_state["exists"]:
        _disable_legacy_service_after_daemon_migration(log=log)
        if legacy_state["enabled"] and autostart_argv:
            log(
                "Migrated enabled PenguinBurner.service autostart intent to "
                f"{unit_path.name}: {shlex.join(autostart_argv)}"
            )
        else:
            log("Migrated existing PenguinBurner.service to penguin-burnerd.service.")
    else:
        log("No existing PenguinBurner.service found; daemon service is ready.")


def read_legacy_service_state() -> dict[str, object]:
    unit_path = legacy_systemd_service_unit_path()
    text = ""
    exists = unit_path.is_file()
    if exists:
        text = unit_path.read_text(encoding="utf-8", errors="replace")
    return {
        "exists": exists,
        "enabled": _systemd_unit_is_enabled(unit_path.name) if exists else False,
        "active": _systemd_unit_is_active(unit_path.name) if exists else False,
        "runtime_argv": parse_runtime_argv_from_unit_text(text),
    }


def parse_runtime_argv_from_unit_text(text: str) -> list[str]:
    exec_start = ""
    for line in str(text).splitlines():
        line = line.strip()
        if line.startswith("ExecStart="):
            exec_start = line.split("=", 1)[1].strip()
            break
    if not exec_start:
        return []
    try:
        parts = shlex.split(exec_start.replace("%%", "%"))
    except ValueError:
        return []
    for index, part in enumerate(parts):
        if Path(part).name == "penguin_burner.py":
            return parts[index + 1 :]
        if Path(part).name == "penguin_burner.sh":
            return parts[index + 1 :]
    return []


def _systemd_unit_is_enabled(unit_name: str) -> bool:
    result = subprocess.run(
        [SYSTEMCTL, "is-enabled", unit_name],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=stable_subprocess_env(),
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "enabled"


def _systemd_unit_is_active(unit_name: str) -> bool:
    result = subprocess.run(
        [SYSTEMCTL, "is-active", unit_name],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=stable_subprocess_env(),
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "active"


def _enable_and_start_or_restart_daemon_unit(unit_name: str) -> None:
    run_checked_subprocess([SYSTEMCTL, "enable", unit_name])
    run_checked_subprocess([SYSTEMCTL, "restart", unit_name])


def _wait_for_daemon_status(socket_path) -> None:
    last_error = None
    for _attempt in range(30):
        try:
            daemon_status(socket_path=socket_path, timeout_s=1)
            return
        except Exception as exc:
            last_error = exc
            time.sleep(0.1)
    raise RuntimeError(f"PenguinBurner daemon did not become reachable: {last_error}")


def _disable_legacy_service_after_daemon_migration(*, log) -> None:
    unit_name = f"{LEGACY_PENGUIN_BURNER_UNIT_NAME}.service"
    result = subprocess.run(
        [SYSTEMCTL, "disable", "--now", unit_name],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=stable_subprocess_env(),
        check=False,
    )
    if result.returncode == 0:
        log(f"Stopped and disabled legacy {unit_name} after daemon migration.")
    subprocess.run(
        [SYSTEMCTL, "reset-failed", unit_name],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=stable_subprocess_env(),
        check=False,
    )


def uninstall_systemd_service(*, log):
    if not systemd_is_available():
        raise RuntimeError("systemd service uninstall is unavailable on this system.")
    if os.geteuid() != 0:
        raise RuntimeError(
            "systemd service uninstall requires root privileges. Re-run with sudo."
        )

    _stop_active_runtime_before_daemon_restart()
    clear_last_runtime_state()  # nothing to re-run once the service is gone
    unit_paths = (
        daemon_systemd_service_unit_path(),
        legacy_systemd_service_unit_path(),
    )
    for unit_path in unit_paths:
        subprocess.run(
            [SYSTEMCTL, "disable", "--now", unit_path.name],
            env=stable_subprocess_env(),
            check=False,
        )
        if unit_path.exists():
            unit_path.unlink()
    run_checked_subprocess([SYSTEMCTL, "daemon-reload"])
    for unit_path in unit_paths:
        subprocess.run(
            [SYSTEMCTL, "reset-failed", unit_path.name],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=stable_subprocess_env(),
            check=False,
        )
    log("Removed penguin-burnerd.service and legacy PenguinBurner.service.")


def _clear_existing_penguin_burner_unit(*, log, reason):
    unit_name = f"{LEGACY_PENGUIN_BURNER_UNIT_NAME}.service"
    unit_path = legacy_systemd_service_unit_path()

    subprocess.run(
        [SYSTEMCTL, "disable", "--now", unit_name],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=stable_subprocess_env(),
        check=False,
    )
    if unit_path.exists():
        unit_path.unlink()
        log(f"Removed existing static {unit_name} before {reason}.")
    run_checked_subprocess([SYSTEMCTL, "daemon-reload"])
    subprocess.run(
        [SYSTEMCTL, "reset-failed", unit_name],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=stable_subprocess_env(),
        check=False,
    )


def clear_existing_penguin_burner_unit_for_daemonize(*, log):
    _clear_existing_penguin_burner_unit(
        log=log,
        reason="transient daemon start",
    )


def clear_existing_penguin_burner_unit_for_install(*, log):
    _clear_existing_penguin_burner_unit(
        log=log,
        reason="persistent service install",
    )


def stop_existing_penguin_burner_runtime(*, log):
    if not systemd_is_available():
        return
    if os.geteuid() != 0:
        return
    unit_name = f"{LEGACY_PENGUIN_BURNER_UNIT_NAME}.service"
    result = subprocess.run(
        [SYSTEMCTL, "stop", unit_name],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=stable_subprocess_env(),
        check=False,
    )
    if result.returncode == 0:
        log(f"Stopped existing {unit_name} before foreground Auto-UV scan.")
    subprocess.run(
        [SYSTEMCTL, "reset-failed", unit_name],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=stable_subprocess_env(),
        check=False,
    )


def daemonize_with_systemd(program_file, argv, *, journal_hours, log):
    if not systemd_is_available():
        raise RuntimeError(
            "systemd background mode is unavailable on this system. "
            "Run PenguinBurner directly or use a systemd-based system."
        )
    if os.geteuid() != 0:
        raise RuntimeError(
            "automatic systemd daemon mode requires root privileges. "
            "Re-run PenguinBurner with sudo."
        )

    binary_changed = install_daemon_binary(program_file)
    clear_existing_penguin_burner_unit_for_daemonize(log=log)
    _ensure_daemon_service_started(
        program_file,
        socket_path=DEFAULT_DAEMON_SOCKET,
        binary_changed=binary_changed,
        log=log,
    )
    result, _spec = _apply_runtime(argv, socket_path=DEFAULT_DAEMON_SOCKET)
    log(
        "Started runtime profile through "
        f"{PENGUIN_BURNER_DAEMON_UNIT_NAME}.service"
        + (
            f" (pid {result.get('pid')})."
            if str(result.get("pid") or "").strip()
            else "."
        )
    )
    log(f"Follow the journal with: {journalctl_follow_command(journal_hours)}")


def _ensure_daemon_service_started(
    program_file, *, socket_path, binary_changed, log
) -> None:
    unit_path = daemon_systemd_service_unit_path()
    unit_text = build_daemon_api_service_unit(program_file, socket_path=socket_path)
    wrote_unit = False
    current_text = (
        unit_path.read_text(encoding="utf-8", errors="replace")
        if unit_path.exists()
        else ""
    )
    if current_text != unit_text:
        unit_path.write_text(unit_text, encoding="utf-8")
        wrote_unit = True
        verb = "Updated" if current_text else "Installed"
        log(f"{verb} {unit_path.name} at {unit_path}.")
    if wrote_unit:
        run_checked_subprocess([SYSTEMCTL, "daemon-reload"])
        clear_last_runtime_state()
    subprocess.run(
        [SYSTEMCTL, "reset-failed", unit_path.name],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=stable_subprocess_env(),
        check=False,
    )
    if wrote_unit:
        run_checked_subprocess([SYSTEMCTL, "enable", unit_path.name])
    if wrote_unit or binary_changed:
        run_checked_subprocess([SYSTEMCTL, "restart", unit_path.name])
    else:
        run_checked_subprocess([SYSTEMCTL, "start", unit_path.name])
    _wait_for_daemon_status(socket_path)
