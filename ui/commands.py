from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import pwd
import shutil
import sys
from typing import Mapping

from auto_uv.scan_mode.auto_uv_mode import (
    ADAPTIVE_TIER_MODES,
    ADAPTIVE_TIER_OPTION_SUFFIXES,
    adaptive_tier_option_key,
)
from ui.constants import DEFAULT_FINAL_VERIFICATION_DURATION_S
from ui.features.tuning.gpu_selection import runtime_gpu_index


FLATPAK_INFO_PATH = Path("/.flatpak-info")
FLATPAK_APP_ID = "io.github.jpietek.PenguinBurner"


def running_in_flatpak() -> bool:
    return FLATPAK_INFO_PATH.is_file()


def cli_base_command() -> list[str]:
    script_path = Path(__file__).resolve().parents[1] / "penguin_burner.py"
    if script_path.is_file():
        return [sys.executable, str(script_path)]
    return ["penguin-burner-cli"]


def host_cli_base_command() -> list[str]:
    override = os.environ.get("PENGUIN_BURNER_HOST_CLI", "").strip()
    if override:
        return [override]
    if running_in_flatpak():
        flatpak = shutil.which("flatpak") or "/usr/bin/flatpak"
        return [
            flatpak,
            "run",
            "--user",
            "--command=penguin-burner-cli",
            os.environ.get("FLATPAK_ID", "").strip() or FLATPAK_APP_ID,
        ]
    return ["penguin-burner-cli"]


def _desktop_user_name() -> str:
    return (
        os.environ.get("PENGUIN_BURNER_Q2RTX_USER", "").strip()
        or os.environ.get("SUDO_USER", "").strip()
        or os.environ.get("USER", "").strip()
        or os.environ.get("LOGNAME", "").strip()
    )


def desktop_user_env() -> list[str]:
    user = _desktop_user_name()
    uid = (
        os.environ.get("PENGUIN_BURNER_Q2RTX_UID", "").strip()
        or os.environ.get("SUDO_UID", "").strip()
    )
    gid = (
        os.environ.get("PENGUIN_BURNER_Q2RTX_GID", "").strip()
        or os.environ.get("SUDO_GID", "").strip()
    )
    if not user and os.getuid() != 0:
        try:
            user = pwd.getpwuid(os.getuid()).pw_name
        except KeyError:
            user = ""
    if not uid and os.getuid() != 0:
        uid = str(os.getuid())
    if not gid and os.getgid() != 0:
        gid = str(os.getgid())

    values = []
    if user:
        values.append(f"PENGUIN_BURNER_Q2RTX_USER={user}")
        values.append(f"SUDO_USER={user}")
    if uid:
        values.append(f"PENGUIN_BURNER_Q2RTX_UID={uid}")
        values.append(f"SUDO_UID={uid}")
    if gid:
        values.append(f"PENGUIN_BURNER_Q2RTX_GID={gid}")
        values.append(f"SUDO_GID={gid}")
    return values


def desktop_session_env() -> list[str]:
    names = [
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XAUTHORITY",
        "XDG_CURRENT_DESKTOP",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
    ]
    values = []
    for name in names:
        if running_in_flatpak() and name == "DBUS_SESSION_BUS_ADDRESS":
            value = _host_session_bus_address()
        else:
            value = os.environ.get(name, "").strip()
        if value:
            values.append(f"{name}={value}")
    if os.environ.get("DISPLAY", "").strip() and not os.environ.get(
        "XAUTHORITY", ""
    ).strip():
        xauthority = _default_xauthority_path()
        if xauthority:
            values.append(f"XAUTHORITY={xauthority}")
    return values


def _default_xauthority_path() -> str:
    user = _desktop_user_name()
    home = ""
    if user:
        try:
            home = pwd.getpwnam(user).pw_dir
        except KeyError:
            home = ""
    if not home:
        return ""
    path = Path(home) / ".Xauthority"
    return str(path) if path.is_file() else ""


def _host_session_bus_address() -> str:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if runtime_dir.startswith("/run/user/"):
        return f"unix:path={runtime_dir}/bus"
    uid = (
        os.environ.get("PENGUIN_BURNER_Q2RTX_UID", "").strip()
        or os.environ.get("SUDO_UID", "").strip()
    )
    if not uid and os.getuid() != 0:
        uid = str(os.getuid())
    return f"unix:path=/run/user/{uid}/bus" if uid else ""


def _privileged_command_base() -> list[str] | None:
    if os.geteuid() == 0:
        return []
    if running_in_flatpak():
        flatpak_spawn = shutil.which("flatpak-spawn")
        if flatpak_spawn:
            return [flatpak_spawn, "--host", "/usr/bin/pkexec", "/usr/bin/env"]
        return None
    escalator = shutil.which("pkexec") or shutil.which("sudo")
    if not escalator:
        return None
    env = shutil.which("env") or "/usr/bin/env"
    return [escalator, env]


def _privileged_env() -> list[str]:
    values = []
    if running_in_flatpak():
        values.extend(_host_flatpak_user_env())
        values.append(_host_path_assignment())
        pythonpath = _host_pythonpath_assignment()
        if pythonpath:
            values.append(pythonpath)
    return [*values, *desktop_user_env(), *desktop_session_env()]


def _host_flatpak_user_env() -> list[str]:
    home = _desktop_user_home()
    if not home:
        return []
    return [
        f"HOME={home}",
        f"XDG_DATA_HOME={home}/.local/share",
    ]


def _desktop_user_home() -> str:
    override = os.environ.get("PENGUIN_BURNER_HOME", "").strip()
    if override:
        return str(Path(override).expanduser())
    user = _desktop_user_name()
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


def _host_path_assignment() -> str:
    entries = []
    home = (_desktop_user_home() if running_in_flatpak() else str(Path.home())).strip()
    if home and home != "/":
        entries.append(str(Path(home) / ".local" / "bin"))
    entries.extend(["/usr/local/bin", "/usr/bin", "/bin"])
    for item in os.environ.get("PATH", "").split(os.pathsep):
        item = item.strip()
        if item and not item.startswith("/app") and item not in entries:
            entries.append(item)
    return "PATH=" + os.pathsep.join(entries)


def _host_pythonpath_assignment() -> str:
    entries = []
    home = Path.home()
    user_lib = home / ".local" / "lib"
    if user_lib.is_dir():
        entries.extend(
            str(path)
            for path in sorted(user_lib.glob("python*/site-packages"), reverse=True)
            if path.is_dir()
        )
    for item in os.environ.get("PYTHONPATH", "").split(os.pathsep):
        item = item.strip()
        if item and not item.startswith("/app") and item not in entries:
            entries.append(item)
    return "PYTHONPATH=" + os.pathsep.join(entries) if entries else ""


def _privileged_command(command: list[str]) -> list[str]:
    base = _privileged_command_base()
    if base is None:
        return list(command)
    if not base:
        return list(command)
    return [*base, *_privileged_env(), *command]


def scan_command(auto_uv_options: Mapping[str, object] | None = None) -> list[str]:
    options = auto_uv_options or {}
    payload = {
        "gpu_index": options.get("gpu_index", runtime_gpu_index()),
    }
    option_keys = (
        "auto_uv_mode",
        "auto_uv_min_voltage_mv",
        "auto_uv_max_clock_drop_pct",
        "auto_uv_memory_offset_mhz",
        "auto_uv_power_limit_w",
        "auto_uv_tail_rise_bins",
        "auto_oc_target_voltage_mv",
        "auto_oc_target_clock_mhz",
        # Per-tier full-scan overrides (adaptive mode only), derived from the
        # canonical tier/option constants so a new option cannot be silently
        # dropped from the daemon payload.
        *(
            adaptive_tier_option_key(tier, suffix)
            for tier in ADAPTIVE_TIER_MODES
            for suffix in ADAPTIVE_TIER_OPTION_SUFFIXES
        ),
    )
    for key in option_keys:
        value = options.get(key)
        if value in (None, ""):
            continue
        payload[key] = value
    return [
        sys.executable,
        "-m",
        "runtime.daemon_client",
        "start-auto-uv",
        json.dumps(payload, separators=(",", ":")),
    ]


def privileged_command(command: list[str]) -> list[str]:
    if running_in_flatpak():
        command = _host_equivalent_command(command)
    return _privileged_command(command)


def _host_equivalent_command(command: list[str]) -> list[str]:
    command = list(command)
    local_base = cli_base_command()
    if command[: len(local_base)] == local_base:
        return [*host_cli_base_command(), *command[len(local_base) :]]
    return command


def daemon_migration_command() -> list[str]:
    if running_in_flatpak():
        return _flatpak_daemon_install_command(autostart_intent=None)
    return privileged_command([*cli_base_command(), "--migrate-to-daemon-service"])


def _flatpak_daemon_restart_and_wait_script() -> str:
    return r"""
daemon_socket=/run/penguin-burnerd.sock

wait_for_penguin_burnerd() {
    attempt=0
    while [ "$attempt" -lt 30 ]; do
        if /usr/bin/python3 - "$daemon_socket" >/dev/null 2>&1 <<'PY'
import json
import socket
import sys

path = sys.argv[1]
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
    client.settimeout(1.0)
    client.connect(path)
    client.sendall(b'{"method":"status"}\n')
    data = b""
    while b"\n" not in data:
        chunk = client.recv(4096)
        if not chunk:
            break
        data += chunk
response = json.loads(data.split(b"\n", 1)[0].decode("utf-8"))
if not response.get("ok"):
    raise SystemExit(1)
PY
        then
            return 0
        fi
        attempt=$((attempt + 1))
        sleep 0.1
    done
    return 1
}

print_penguin_burnerd_diagnostics() {
    systemctl status --no-pager penguin-burnerd.service >&2 || true
    journalctl -u penguin-burnerd.service -n 80 --no-pager >&2 || true
}

restart_penguin_burnerd() {
    rm -f "$daemon_socket"
    if ! systemctl restart penguin-burnerd.service; then
        echo "Failed to restart penguin-burnerd.service." >&2
        print_penguin_burnerd_diagnostics
        return 1
    fi
    if wait_for_penguin_burnerd; then
        return 0
    fi
    echo "penguin-burnerd.service restarted, but $daemon_socket did not answer status." >&2
    print_penguin_burnerd_diagnostics
    return 1
}
""".strip()


def runtime_profile_command(
    action: str,
    *,
    profile_selector: str = "",
    silent_fan_curve: bool = False,
    adaptive_auto_uv: bool = False,
    persist_on_startup: bool = False,
    gpu_index: int | None = None,
) -> list[str]:
    if action == "clear-boot":
        return [
            sys.executable,
            "-m",
            "runtime.daemon_client",
            "clear-boot-runtime-spec",
        ]
    intent = {
        "profile_selector": str(profile_selector or "").strip(),
        "silent_fan_curve": bool(silent_fan_curve),
        "adaptive_auto_uv": bool(adaptive_auto_uv),
        "gpu_index": None if gpu_index is None else max(0, int(gpu_index)),
    }
    runtime_argv: list[str] = []
    if action == "daemonize":
        service_flag = "--daemonize"
    elif action == "install-systemd":
        service_flag = "--install-systemd-service"
    elif action == "uninstall-systemd":
        service_flag = "--uninstall-systemd-service"
    else:
        raise ValueError(f"unknown runtime profile action: {action}")
    if profile_selector and action != "uninstall-systemd":
        runtime_argv.extend(["--auto-uv-profile", str(profile_selector)])
    if silent_fan_curve and action != "uninstall-systemd":
        runtime_argv.append("--silent-fan-curve")
    if adaptive_auto_uv and action != "uninstall-systemd":
        runtime_argv.append("--adaptive-auto-uv")
    if gpu_index is not None:
        runtime_argv.extend(["--gpu-index", str(max(0, int(gpu_index)))])
    if action == "daemonize":
        return _daemon_runtime_profile_command(
            intent,
            persist_on_startup=persist_on_startup,
        )
    if running_in_flatpak():
        return _flatpak_systemd_profile_command(action, runtime_argv, intent)
    command = [*cli_base_command(), service_flag, *runtime_argv]
    return privileged_command(command)


def _daemon_runtime_profile_command(
    intent: dict,
    *,
    persist_on_startup: bool = False,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "runtime.daemon_client",
        "apply-runtime-intent",
    ]
    if persist_on_startup:
        command.append("--boot")
    command.append(json.dumps(intent, separators=(",", ":")))
    return command


def _flatpak_systemd_profile_command(
    action: str,
    runtime_argv: list[str],
    intent: dict,
) -> list[str]:
    if action == "install-systemd":
        return _flatpak_daemon_install_command(
            autostart_intent=dict(intent) if runtime_argv else {}
        )
    if action == "uninstall-systemd":
        return _flatpak_uninstall_systemd_command()
    raise ValueError(f"unknown runtime profile action: {action}")


def _flatpak_daemon_install_command(
    *, autostart_intent: dict | None
) -> list[str]:
    """Elevated host-side install of the Rust daemon from a Flatpak sandbox.

    The manifest builds penguin-burnerd into /app/libexec (host-visible inside
    the flatpak deployment dir); the one pkexec elevation copies it onto the
    root-owned /usr/libexec/penguin-burnerd (a root service must never execute
    from a user-writable path), drops the generated unit into
    /etc/systemd/system, and enables+restarts it. ExecStart points at the fixed
    /usr/libexec path, so unit generation needs no flatpak-specific ExecStart.

    ``autostart_intent`` is None for migration/repair, an empty dict for no boot
    runtime, or a semantic intent that the newly installed daemon resolves and
    persists through its typed API.
    """
    from runtime.support.runtime_service import (
        BOOT_RUNTIME_STATE_PATH,
        LAST_RUNTIME_STATE_PATH,
        LIBEXEC_DAEMON_BINARY,
        build_daemon_api_service_unit,
        flatpak_host_app_path,
        flatpak_host_cli_program_file,
        flatpak_host_site_packages_path,
    )

    program_file = flatpak_host_cli_program_file()
    unit = build_daemon_api_service_unit(
        program_file,
        binary_path=str(LIBEXEC_DAEMON_BINARY),
    )
    encoded_unit = base64.b64encode(unit.encode("utf-8")).decode("ascii")
    daemon_binary_src = flatpak_host_app_path() / "libexec" / "penguin-burnerd"

    environment = [
        f"PENGUIN_BURNER_DAEMON_BINARY_SRC={daemon_binary_src}",
        f"PENGUIN_BURNER_SYSTEMD_UNIT_B64={encoded_unit}",
    ]
    runtime_block = ""
    if autostart_intent:
        intent_json = json.dumps(autostart_intent, separators=(",", ":"))
        environment.extend(
            [
                "PENGUIN_BURNER_RUNTIME_INTENT_B64="
                + base64.b64encode(intent_json.encode("utf-8")).decode("ascii"),
                f"PENGUIN_BURNER_RUNTIME_PYTHONPATH={flatpak_host_site_packages_path()}",
                f"PENGUIN_BURNER_RUNTIME_HOME={_desktop_user_home()}",
            ]
        )
        runtime_block = (
            "\nintent_json=\"$(printf '%s' \"$PENGUIN_BURNER_RUNTIME_INTENT_B64\" | base64 -d)\"\n"
            "env PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 "
            "PYTHONPATH=\"$PENGUIN_BURNER_RUNTIME_PYTHONPATH\" "
            "PENGUIN_BURNER_HOME=\"$PENGUIN_BURNER_RUNTIME_HOME\" "
            "/usr/bin/python3 -m runtime.daemon_client apply-runtime-intent "
            "--boot \"$intent_json\"\n"
        )

    script = "\n".join(
        [
            _flatpak_daemon_restart_and_wait_script(),
            (
                r"""
legacy_unit=/etc/systemd/system/PenguinBurner.service
unit=/etc/systemd/system/penguin-burnerd.service
daemon_dir=/usr/libexec
daemon_target="$daemon_dir/penguin-burnerd"
daemon_tmp=
unit_tmp=
cleanup_install_temps() {
    if [ -n "$daemon_tmp" ]; then rm -f -- "$daemon_tmp"; fi
    if [ -n "$unit_tmp" ]; then rm -f -- "$unit_tmp"; fi
}
trap cleanup_install_temps EXIT
trap 'exit 1' HUP INT TERM
if [ ! -e "$daemon_dir" ]; then
    mkdir -m0755 "$daemon_dir"
fi
if [ ! -d "$daemon_dir" ] || [ -L "$daemon_dir" ] || \
   [ "$(stat -c %u "$daemon_dir")" != 0 ] || \
   [ $((0$(stat -c %a "$daemon_dir") & 0022)) -ne 0 ]; then
    echo "$daemon_dir is not a safe root-owned install directory." >&2
    exit 1
fi
daemon_tmp="$(mktemp "$daemon_dir/.penguin-burnerd.XXXXXX")"
dd if="$PENGUIN_BURNER_DAEMON_BINARY_SRC" of="$daemon_tmp" \
   iflag=nofollow,fullblock,nonblock oflag=nofollow conv=fsync status=none
if [ "$(od -An -tx1 -N4 "$daemon_tmp" | tr -d '[:space:]')" != 7f454c46 ]; then
    echo "PenguinBurner daemon install source is not an ELF binary." >&2
    exit 1
fi
chown root:root "$daemon_tmp"
chmod 0755 "$daemon_tmp"
unit_tmp="$(mktemp /etc/systemd/system/.penguin-burnerd.service.XXXXXX)"
printf '%s' "$PENGUIN_BURNER_SYSTEMD_UNIT_B64" | base64 -d > "$unit_tmp"
chmod 0644 "$unit_tmp"
mv -f -- "$daemon_tmp" "$daemon_target"
daemon_tmp=
mv -f -- "$unit_tmp" "$unit"
unit_tmp=
systemctl disable --now PenguinBurner.service >/dev/null 2>&1 || true
if [ -f "$legacy_unit" ]; then
    rm -f "$legacy_unit"
    echo "Removed existing static PenguinBurner.service."
fi
trap - EXIT HUP INT TERM
"""
                + (
                    f'\nrm -f "{LAST_RUNTIME_STATE_PATH}" '
                    f'"{BOOT_RUNTIME_STATE_PATH}" '
                    '/run/penguin-burner/active-runtime.json\n'
                )
                + r"""
systemctl daemon-reload
systemctl reset-failed PenguinBurner.service >/dev/null 2>&1 || true
systemctl reset-failed penguin-burnerd.service >/dev/null 2>&1 || true
systemctl enable penguin-burnerd.service
restart_penguin_burnerd
"""
                + runtime_block
                + r"""
echo "Copied penguin-burnerd to /usr/libexec/penguin-burnerd."
echo "Installed and enabled penguin-burnerd.service at $unit."
echo "Follow the journal with: journalctl -u penguin-burnerd.service --since \"-4 hours\" -f"
""".strip()
            ),
        ]
    )
    return _privileged_command(
        [
            *environment,
            "/bin/sh",
            "-eu",
            "-c",
            script,
            "penguin-burner-daemon-install",
        ]
    )


def _flatpak_uninstall_systemd_command() -> list[str]:
    script = r"""
legacy_unit=/etc/systemd/system/PenguinBurner.service
daemon_unit=/etc/systemd/system/penguin-burnerd.service
last_runtime_state=/var/lib/penguin-burner/last-runtime.json
boot_runtime_state=/var/lib/penguin-burner/boot-runtime.json
active_runtime_state=/run/penguin-burner/active-runtime.json
systemctl disable --now PenguinBurner.service >/dev/null 2>&1 || true
systemctl disable --now penguin-burnerd.service >/dev/null 2>&1 || true
rm -f "$legacy_unit" "$daemon_unit" "$last_runtime_state" "$boot_runtime_state" "$active_runtime_state"
systemctl daemon-reload
systemctl reset-failed PenguinBurner.service >/dev/null 2>&1 || true
systemctl reset-failed penguin-burnerd.service >/dev/null 2>&1 || true
echo "Removed PenguinBurner.service and penguin-burnerd.service."
""".strip()
    return _privileged_command(
        [
            "/bin/sh",
            "-eu",
            "-c",
            script,
            "penguin-burner-systemd-uninstall",
        ]
    )


def profile_verify_command(
    *,
    profile_selector: str = "",
    duration_s: int = DEFAULT_FINAL_VERIFICATION_DURATION_S,
    stop_request_path: str | Path = "",
    gpu_index: int | None = None,
) -> list[str]:
    # Verification runs inside the already-root daemon (streaming socket RPC,
    # no pkexec). The daemon owns the child's --stability-stop-request-file
    # marker (same <config>/profile-verify-stop-requested path the UI writes
    # for a cooperative stop), so stop_request_path is accepted for caller
    # compatibility but not forwarded.
    del stop_request_path
    payload: dict[str, object] = {
        "stability_seconds": max(1, int(duration_s)),
        "gpu_index": runtime_gpu_index() if gpu_index is None else max(0, int(gpu_index)),
    }
    if profile_selector:
        payload["auto_uv_profile"] = str(profile_selector)
    return [
        sys.executable,
        "-m",
        "runtime.daemon_client",
        "start-profile-verification",
        json.dumps(payload, separators=(",", ":")),
    ]


def delete_profiles_command(profile_paths: list[str]) -> list[str]:
    # Profile deletion goes through the root daemon (unary RPC, no pkexec);
    # the daemon path-validates against the saved-profiles dir before deleting.
    return [
        sys.executable,
        "-m",
        "runtime.daemon_client",
        "delete-auto-uv-profiles",
        json.dumps([str(path) for path in profile_paths], separators=(",", ":")),
    ]
