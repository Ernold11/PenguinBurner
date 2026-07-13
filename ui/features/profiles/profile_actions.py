from __future__ import annotations

import shlex

from common.penguin_burner_paths import default_user_config_dir
from curve_editors.uv.vf_curve_manual_editor import editable_anchor_from_profile
from profiles.uv.profile_store import STOCK_PROFILE_SELECTOR
from profiles.uv.profile_store import delete_auto_uv_profile_paths
from profiles.uv.profile_tiers import save_profile_tier_assignment
from profiles.uv.profile_tiers import save_profile_tier_none_assignment

from ui.commands import delete_profiles_command
from ui.commands import profile_verify_command
from ui.commands import runtime_profile_command
from ui.daemon_setup import ensure_daemon_ready_for_privileged_action
from ui.components.fan_curve_editor import open_fan_curve_editor_dialog
from ui.components.vf_curve_editor import open_vf_curve_editor_dialog
from ui.features.curves.curve_profiles import profile_base_curve_points
from ui.features.curves.curve_profiles import profile_curve_plan
from ui.features.curves.curve_profiles import save_edited_curve_profile
from ui.dialogs.verify import select_verify_options
from ui.features.curves.fan_profiles import profile_fan_curve_points
from ui.features.curves.fan_profiles import profile_fan_curve_target_point
from ui.features.curves.fan_profiles import profile_fan_measurement_points
from ui.features.curves.fan_profiles import profile_id_from_archive_path
from ui.features.curves.fan_profiles import save_edited_fan_profile
from ui.features.curves.fan_profiles import sync_profile_fan_payload
from ui.features.integrations.lact_export import detect_lact_gpu_id
from ui.features.integrations.lact_export import lact_export_output_path
from ui.features.integrations.lact_export import write_lact_profile_config
from ui.features.profiles.profiles import adaptive_profile_tier_labels
from ui.features.profiles.profiles import delete_confirmation_text
from ui.features.profiles.profiles import profile_can_apply
from ui.features.profiles.profiles import profile_can_verify
from ui.features.profiles.profiles import profile_delete_autostart_action
from ui.features.profiles.profiles import profile_for_selector
from ui.features.profiles.profiles import profile_is_deletable
from ui.features.profiles.profiles import profile_status_label
from ui.features.profiles.profiles import profile_verify_selector
from ui.features.profiles.profiles import systemd_autostart_profile_info
from ui.features.tuning.verify import stop_request_path as verify_stop_request_path
from ui.features.tuning.verify import workload_label


class ProfileActionsMixin:
    def _apply_profile_action(self) -> str:
        # Apply always targets the already-root daemon. Persistence is a daemon
        # API flag on that request; checking for the host unit from a Flatpak is
        # unreliable because the sandbox cannot see the host's /etc/systemd.
        # If the daemon is genuinely unavailable, the migration gate performs
        # the one allowed systemd lifecycle elevation before this request runs.
        return "daemonize"

    def _run_profiles(self) -> None:
        self._run_runtime_action(self._apply_profile_action())

    def _run_runtime_action(
        self,
        action: str,
        *,
        adaptive_auto_uv: bool = False,
        profile_selector: str = "",
    ) -> None:
        if self._workflow_running():
            return
        no_profile_actions = {"clear-boot", "uninstall-systemd"}
        profile_id = str(profile_selector or "").strip()
        selected_profile = None
        if action not in no_profile_actions and adaptive_auto_uv:
            tiers = adaptive_profile_tier_labels(self.profile_summaries)
            if not tiers:
                available = ", ".join(tiers) if tiers else "none"
                self.errors.show(
                    "Adaptive Auto-UV unavailable",
                    "Adaptive Auto-UV requires at least one verified Auto-UV "
                    f"profile. Available tiers: {available}.",
                )
                return
            selected_profile = profile_for_selector(self.profile_summaries, "latest")
        elif action not in no_profile_actions:
            profile_id = profile_id or self.profile_list.selected_profile_id()
            if not profile_id:
                self.log_view.append("\nNo profile selected.\n")
                return
            selected_profile = profile_for_selector(self.profile_summaries, profile_id)
            if not profile_can_apply(selected_profile or {}):
                message = "This edited profile must be verified before it can be applied."
                self.controls.set_status_text(message)
                self.log_view.append(f"\n{message}\n")
                return
        if (
            action not in no_profile_actions
            and selected_profile
            and self.profile_list.silent_fan_enabled()
            and not sync_profile_fan_payload(selected_profile)
        ):
            self.controls.set_status_text("No runtime-ready silent fan curve is available.")
            self.log_view.append("\nNo runtime-ready silent fan curve is available.\n")
            return
        if action in {
            "clear-boot",
            "daemonize",
        } and not ensure_daemon_ready_for_privileged_action(
            QtWidgets=self.QtWidgets,
            parent=self.window,
            log=self.log_view.append,
            action_label=(
                "Disabling the boot profile"
                if action == "clear-boot"
                else (
                    "Applying adaptive Auto-UV"
                    if adaptive_auto_uv
                    else "Applying runtime profile"
                )
            ),
        ):
            return
        # A standing action is always complete: Apply persists the selected
        # profile for boot, while Restore defaults persists the stock runtime.
        persist_on_startup = action not in no_profile_actions
        command = runtime_profile_command(
            action,
            profile_selector="" if action in no_profile_actions else profile_id,
            silent_fan_curve=self.profile_list.silent_fan_enabled(),
            adaptive_auto_uv=adaptive_auto_uv,
            persist_on_startup=persist_on_startup,
            gpu_index=self.gpu_index,
        )
        if action not in no_profile_actions:
            self._persist_silent_fan_preference(self.profile_list.silent_fan_enabled())
        if action not in no_profile_actions:
            # A profile is now in effect, so the GPU is no longer at stock.
            self._defaults_restored = False
        self.controls.set_status_text(
            self._runtime_action_start_text(
                action,
                adaptive_auto_uv=adaptive_auto_uv,
                persist_on_startup=persist_on_startup,
            )
        )
        self._set_profile_actions_enabled(False)
        self.controls.start_button.setEnabled(False)
        self.command_controller.start(
            f"adaptive-{action}" if adaptive_auto_uv else action,
            command,
            fail_text="Failed to start runtime profile action.",
        )

    def _restore_gpu_defaults(self) -> None:
        if self._workflow_running():
            return
        # Elevated work goes through the already-root daemon (NO pkexec): ask it
        # to run the reserved stock runtime. The daemon stops the current profile,
        # resets the GPU to factory, applies no undervolt, and keeps running for
        # fan control. See CLAUDE.md: privileged ops route through the daemon.
        if not ensure_daemon_ready_for_privileged_action(
            QtWidgets=self.QtWidgets,
            parent=self.window,
            log=self.log_view.append,
            action_label="Restoring GPU to stock",
        ):
            return
        command = runtime_profile_command(
            "daemonize",
            profile_selector=STOCK_PROFILE_SELECTOR,
            silent_fan_curve=self.profile_list.silent_fan_enabled(),
            persist_on_startup=True,
            gpu_index=self.gpu_index,
        )
        self.controls.set_status_text(
            "Restoring GPU to stock now and at boot (daemon keeps running)."
        )
        self._set_profile_actions_enabled(False)
        self.controls.start_button.setEnabled(False)
        self.command_controller.start(
            "restore-keep-stock",
            command,
            fail_text="Failed to restore GPU to stock.",
        )

    def _run_delete_autostart_followup(self, *, restore_stock: bool) -> bool:
        self._delete_restore_stock = False
        if restore_stock:
            self._restore_gpu_defaults()
            return True
        return False

    def _edit_profile_fan_curve(self, profile: dict) -> None:
        curve_points = profile_fan_curve_points(profile)
        if self.pg is None or not curve_points:
            self.QtWidgets.QMessageBox.information(
                self.window,
                "Edit Fan Curve",
                "No editable fan curve is available for this profile.",
            )
            return

        def save_edit(edit) -> str:
            path, _payload = save_edited_fan_profile(
                profile,
                edit,
                original_points=curve_points,
            )
            profile_id = profile_id_from_archive_path(path)
            if profile_id:
                self.profile_list.select_profile(profile_id)
            self._load_profiles()
            return f"Saved edited fan curve: {path.name}."

        open_fan_curve_editor_dialog(
            QtCore=self.QtCore,
            QtGui=self.QtGui,
            QtWidgets=self.QtWidgets,
            pg=self.pg,
            parent=self.window,
            curve_points=curve_points,
            measured_points=profile_fan_measurement_points(profile),
            target_point=profile_fan_curve_target_point(profile),
            save_callback=save_edit,
        )

    def _edit_profile_vf_curve(self, profile: dict) -> None:
        plan = profile_curve_plan(profile)
        anchor = editable_anchor_from_profile(profile)
        if not plan or anchor is None:
            self.QtWidgets.QMessageBox.information(
                self.window,
                "Edit VF Curve",
                "No editable V/F curve is available for this profile.",
            )
            return
        manual_edit = profile.get("manual_edit")
        control_voltage_mvs = _manual_curve_control_voltage_mvs(manual_edit)

        def save_edit(edit) -> str:
            path, payload = save_edited_curve_profile(
                profile,
                edit,
                original_anchor_voltage_mv=int(anchor[0]),
                original_anchor_clock_mhz=int(anchor[1]),
            )
            self.last_auto_uv_candidate_id = str(payload.get("candidate_id", "")).strip()
            profile_id = profile_id_from_archive_path(path)
            self.controls.set_status_text(
                f"Saved edited curve draft: {path.name}. Verify it before Apply."
            )
            self._load_profiles()
            if profile_id:
                self.profile_list.select_profile(profile_id)
            return f"Saved edited curve draft: {path.name}. Verify it before Apply."

        open_vf_curve_editor_dialog(
            QtCore=self.QtCore,
            QtGui=self.QtGui,
            QtWidgets=self.QtWidgets,
            pg=self.pg,
            parent=self.window,
            plan=plan,
            base_points=profile_base_curve_points(profile)
            or self.curve_tabs.base_curve_points,
            anchor=anchor,
            save_callback=save_edit,
            control_voltage_mvs=control_voltage_mvs,
        )

    def _export_lact_profile(self, profile: dict) -> None:
        directory = self.QtWidgets.QFileDialog.getExistingDirectory(
            self.window,
            "Choose LACT Export Directory",
            str(default_user_config_dir()),
        )
        if not directory:
            return
        output_path = lact_export_output_path(directory)
        gpu_id = detect_lact_gpu_id(output_path.parent)
        if not gpu_id:
            self.errors.show(
                "Export LACT",
                "Could not detect the LACT GPU id. Start LACT once, or choose a "
                "directory that already contains config.yaml.",
            )
            return
        include_fan_curve = self.profile_list.silent_fan_enabled()
        if include_fan_curve and not sync_profile_fan_payload(profile):
            self.errors.show(
                "Export LACT",
                "No runtime-ready silent fan curve is available for this profile.",
            )
            return
        try:
            written_path, warnings = write_lact_profile_config(
                profile,
                output_path=output_path,
                gpu_id=gpu_id,
                include_fan_curve=include_fan_curve,
            )
        except Exception as exc:
            self.errors.show("Export LACT", f"LACT export failed:\n{exc}")
            return
        message = f"LACT profile successfully written:\n{written_path}"
        if warnings:
            message += "\n\nWarnings:\n" + "\n".join(str(item) for item in warnings)
        self.controls.set_status_text(f"LACT profile written: {written_path}")
        self.log_view.append("\n" + message + "\n")
        self.QtWidgets.QMessageBox.information(self.window, "Export LACT", message)

    def _verify_profile(self, profile: dict) -> None:
        if self._workflow_running() or not profile_can_verify(profile):
            return
        label = profile_status_label(
            self.profile_summaries,
            str(profile.get("profile_id", "")),
        )
        options = select_verify_options(
            QtWidgets=self.QtWidgets,
            parent=self.window,
            profile_label=label,
        )
        if options is None:
            return
        if not ensure_daemon_ready_for_privileged_action(
            QtWidgets=self.QtWidgets,
            parent=self.window,
            log=self.log_view.append,
            action_label="Verifying an Auto-UV profile",
        ):
            return
        duration_s = int(options["duration_s"])
        command = profile_verify_command(
            profile_selector=profile_verify_selector(profile),
            duration_s=duration_s,
            stop_request_path=verify_stop_request_path(),
            gpu_index=self.gpu_index,
        )
        workload = workload_label()
        self.header.set_stage("Profile verification")
        self.header.set_candidate(label)
        self.controls.set_status_text(f"Verifying {label} with {workload}.")
        self.tabs.setCurrentIndex(self.auto_uv_tab_index)
        self.controls.set_verify_progress(
            0,
            elapsed_s=0,
            target_s=duration_s,
            detail=f"Verifying {label} with {workload}.",
        )
        self.log_view.append(
            "\n$ " + " ".join(shlex.quote(part) for part in command) + "\n"
        )
        self.controls.set_running(True)
        self._set_profile_actions_enabled(False)
        self.verify_controller.start(command, duration_s=duration_s)

    def _delete_selected_profiles(self) -> None:
        if self._workflow_running():
            return
        selected_ids = set(self.profile_list.selected_profile_ids())
        selected_paths = self.profile_list.selected_profile_paths()
        if not selected_paths:
            return
        autostart_info = systemd_autostart_profile_info()
        autostart_action = profile_delete_autostart_action(
            self.profile_summaries,
            list(selected_ids),
            autostart_info,
        )
        restore_stock = autostart_action.get("action") == "restore-stock"
        if not self._confirm_profile_delete(
            restore_stock=restore_stock,
            removes_last_usable_adaptive_profile=(
                autostart_action.get("reason") == "last-usable-adaptive-profile"
            ),
        ):
            return
        try:
            deleted = delete_auto_uv_profile_paths(selected_paths) if selected_paths else []
        except PermissionError:
            self._run_privileged_profile_delete(
                selected_paths,
                selected_ids,
                restore_stock=restore_stock,
            )
            return
        count = len(deleted)
        label = "profile" if count == 1 else "profiles"
        self.log_view.append(f"\nDeleted {count} saved {label}.\n")
        self.controls.set_status_text(f"Deleted {count} saved {label}.")
        self._load_profiles()
        self._run_delete_autostart_followup(restore_stock=restore_stock)

    def _run_privileged_profile_delete(
        self,
        selected_paths: list[str],
        selected_ids: set[str],
        *,
        restore_stock: bool,
    ) -> None:
        if not ensure_daemon_ready_for_privileged_action(
            QtWidgets=self.QtWidgets,
            parent=self.window,
            log=self.log_view.append,
            action_label="Deleting saved Auto-UV profiles",
        ):
            return
        self._delete_restore_stock = bool(restore_stock)
        self._set_profile_actions_enabled(False)
        self.controls.start_button.setEnabled(False)
        self.controls.set_status_text("Deleting selected Auto-UV profiles.")
        self.command_controller.start(
            "delete-profiles",
            delete_profiles_command(selected_paths),
            fail_text="Failed to start Auto-UV profile delete action.",
        )

    def _command_finished(self, kind: str, exit_code, exit_status) -> None:
        label = _runtime_action_label(kind)
        self.log_view.append(f"\n{label} finished: exit_code={exit_code}\n")
        success = int(exit_code) == 0
        self.controls.start_button.setEnabled(not self.scan_controller.is_running())
        self._set_profile_actions_enabled(not self.scan_controller.is_running())
        if kind == "delete-profiles":
            if success:
                self.controls.set_status_text("Selected Auto-UV profiles deleted.")
                self._load_profiles()
                if self._run_delete_autostart_followup(
                    restore_stock=self._delete_restore_stock,
                ):
                    return
            else:
                self.controls.set_status_text("Auto-UV profile deletion failed.")
                self.errors.show_process(
                    title="Profile deletion failed",
                    action_label="Delete selected profiles",
                    exit_code=exit_code,
                    exit_status=exit_status,
                )
            self._delete_restore_stock = False
            self._load_profiles()
            return
        if kind == "restore-keep-stock" and success:
            # Daemon now runs the stock runtime (GPU at factory, no undervolt);
            # reflect that as Default in the status line. Live daemon detection
            # (running selector == stock sentinel) also drives this.
            self._defaults_restored = True
        if success:
            self.controls.set_status_text(f"{label} complete.")
        else:
            self.controls.set_status_text(f"{label} failed.")
            self.errors.show_process(
                title=f"{label} failed",
                action_label=label,
                exit_code=exit_code,
                exit_status=exit_status,
            )
        self._load_profiles()

    def _confirm_profile_delete(
        self,
        *,
        restore_stock: bool,
        removes_last_usable_adaptive_profile: bool = False,
    ) -> bool:
        buttons = (
            self.QtWidgets.QMessageBox.StandardButton.Yes
            | self.QtWidgets.QMessageBox.StandardButton.No
        )
        answer = self.QtWidgets.QMessageBox.question(
            self.window,
            "Delete Profiles",
            delete_confirmation_text(
                self.profile_list.selected_profile_names(),
                restores_stock=restore_stock,
                removes_last_usable_adaptive_profile=(
                    removes_last_usable_adaptive_profile
                ),
            ),
            buttons,
            self.QtWidgets.QMessageBox.StandardButton.No,
        )
        return answer == self.QtWidgets.QMessageBox.StandardButton.Yes

    def _show_profile_context_menu(self, position) -> None:
        table = self.profile_list.table
        index = table.indexAt(position)
        if not index.isValid():
            return
        table.selectRow(int(index.row()))
        profile = self._profile_from_row(int(index.row()))
        if profile is None:
            return
        menu = self.QtWidgets.QMenu(table)
        edit_curve_action = menu.addAction("Edit VF Curve")
        edit_curve_action.setEnabled(bool(profile_curve_plan(profile)))
        fan_action = menu.addAction("Edit Fan Curve")
        fan_action.setEnabled(bool(profile_fan_curve_points(profile)))
        apply_action = menu.addAction("Apply")
        apply_action.setEnabled(not self._workflow_running() and profile_can_apply(profile))
        verify_action = menu.addAction("Verify")
        verify_action.setEnabled(
            not self._workflow_running() and profile_can_verify(profile)
        )
        export_action = menu.addAction("Export LACT")
        export_action.setEnabled(not self._workflow_running() and profile_can_apply(profile))
        tier_menu = menu.addMenu("Assign Tier")
        tier_actions = {
            "efficiency": tier_menu.addAction("Efficiency"),
            "balanced": tier_menu.addAction("Balanced"),
            "performance": tier_menu.addAction("Performance"),
        }
        tier_menu.addSeparator()
        none_tier_action = tier_menu.addAction("None")
        can_assign_tier = (
            not self._workflow_running()
            and profile_can_apply(profile)
            and bool(str(profile.get("profile_id") or "").strip())
        )
        tier_menu.setEnabled(can_assign_tier)
        menu.addSeparator()
        delete_action = menu.addAction("Delete")
        delete_action.setEnabled(
            not self._workflow_running() and profile_is_deletable(profile)
        )
        chosen = menu.exec(table.viewport().mapToGlobal(position))
        if chosen == edit_curve_action:
            self._edit_profile_vf_curve(profile)
        elif chosen == fan_action:
            self._edit_profile_fan_curve(profile)
        elif chosen == apply_action:
            self._run_runtime_action("daemonize")
        elif chosen == verify_action:
            self._verify_profile(profile)
        elif chosen == export_action:
            self._export_lact_profile(profile)
        elif chosen in set(tier_actions.values()):
            profile_id = str(profile.get("profile_id") or "").strip()
            for tier, action in tier_actions.items():
                if chosen == action:
                    save_profile_tier_assignment(profile_id, tier)
                    self._load_profiles()
                    break
        elif chosen == none_tier_action:
            save_profile_tier_none_assignment(str(profile.get("profile_id") or ""))
            self._load_profiles()
        elif chosen == delete_action:
            self._delete_selected_profiles()

    def _profile_from_row(self, row: int) -> dict | None:
        item = self.profile_list.table.item(int(row), 0)
        if item is None:
            return None
        profile_id = str(item.data(self.profile_list.PROFILE_ID_ROLE) or "")
        return profile_for_selector(self.profile_summaries, profile_id)

    def _runtime_action_start_text(
        self,
        action: str,
        *,
        adaptive_auto_uv: bool = False,
        persist_on_startup: bool | None = None,
    ) -> str:
        persists = (
            action == "install-systemd"
            if persist_on_startup is None
            else bool(persist_on_startup)
        )
        if adaptive_auto_uv:
            if persists:
                return "Starting adaptive Auto-UV; Autostart: Yes."
            return "Starting adaptive Auto-UV; Autostart: No."
        selected = self.profile_list.selected_profile_name() or "none"
        if persists:
            return f"Starting profile: {selected}; Autostart: Yes."
        if action in {"clear-boot", "uninstall-systemd"}:
            return "Disabling profile at boot."
        return f"Starting profile: {selected}; Autostart: No."


def _manual_curve_control_voltage_mvs(manual_edit) -> tuple[int, ...]:
    if not isinstance(manual_edit, dict):
        return ()
    raw_values = manual_edit.get("control_voltage_mvs")
    if not isinstance(raw_values, (list, tuple)):
        return ()
    values = []
    seen = set()
    for raw_value in raw_values:
        try:
            value = int(round(float(raw_value)))
        except (TypeError, ValueError):
            continue
        if value in seen:
            continue
        seen.add(value)
        values.append(value)
    return tuple(values)


def _runtime_action_label(action: str) -> str:
    labels = {
        "daemonize": "Apply selected profile",
        "adaptive-daemonize": "Apply adaptive Auto-UV",
        "install-systemd": "Install startup profile",
        "adaptive-install-systemd": "Install adaptive startup profile",
        "clear-boot": "Disable profile at boot",
        "uninstall-systemd": "Disable profile at boot",
        "delete-profiles": "Delete selected profiles",
        "restore-defaults": "Restore GPU defaults",
        "restore-keep-stock": "Keep GPU at stock",
    }
    return labels.get(str(action), str(action) or "Profile action")
