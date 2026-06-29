from __future__ import annotations

import shlex
import subprocess
from typing import Callable

from runtime.daemon_client import daemon_status
from ui.commands import daemon_migration_command


def ensure_daemon_ready_for_privileged_action(
    *,
    QtWidgets,
    parent,
    log,
    action_label: str,
    status_check: Callable[[], dict] = daemon_status,
    command_factory: Callable[[], list[str]] = daemon_migration_command,
    run_command: Callable = subprocess.run,
) -> bool:
    try:
        status_check()
        return True
    except Exception as exc:
        unavailable_reason = str(exc)

    answer = QtWidgets.QMessageBox.question(
        parent,
        "PenguinBurner Hardware Service",
        (
            f"{action_label} requires the PenguinBurner root hardware service.\n\n"
            "PenguinBurner can install or repair it now. If an older "
            "PenguinBurner.service exists, it will be migrated once. This may "
            "ask for your administrator password once."
        ),
        QtWidgets.QMessageBox.StandardButton.Yes
        | QtWidgets.QMessageBox.StandardButton.No,
        QtWidgets.QMessageBox.StandardButton.Yes,
    )
    if answer != QtWidgets.QMessageBox.StandardButton.Yes:
        log(
            "\nPenguinBurner hardware service setup was cancelled. "
            f"Previous status: {unavailable_reason}\n"
        )
        return False

    command = command_factory()
    log("\n$ " + " ".join(shlex.quote(part) for part in command) + "\n")
    result = run_command(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    output = "\n".join(
        part.strip()
        for part in (getattr(result, "stdout", ""), getattr(result, "stderr", ""))
        if str(part or "").strip()
    )
    if output:
        log(output + "\n")
    if int(getattr(result, "returncode", 1)) != 0:
        QtWidgets.QMessageBox.critical(
            parent,
            "PenguinBurner Hardware Service",
            "PenguinBurner could not install or repair the hardware service.\n\n"
            + (output or f"Exit code: {getattr(result, 'returncode', 1)}"),
        )
        return False

    try:
        status_check()
    except Exception as exc:
        QtWidgets.QMessageBox.critical(
            parent,
            "PenguinBurner Hardware Service",
            "The hardware service command finished, but the daemon is not reachable.\n\n"
            f"{exc}",
        )
        return False
    return True
