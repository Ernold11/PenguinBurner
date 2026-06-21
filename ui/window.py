from __future__ import annotations

from pathlib import Path
import shlex

from cli.runtime_config_file import (
    persist_on_startup_from_runtime_config,
    persist_on_startup_to_runtime_config,
    silent_fan_curve_from_runtime_config,
    silent_fan_curve_to_runtime_config,
)
from common.penguin_burner_paths import default_user_config_dir

from ui.features.integrations.afterburner_workflow import AfterburnerImportWorkflow
from ui.commands import runtime_profile_command
from ui.commands import scan_command
from ui.components import CurvePlot
from ui.components import LogView
from ui.components import OverlayConfigPanel
from ui.components import ProfileList
from ui.components import RunsTable
from ui.components import ScanControls
from ui.components import StatusHeader
from ui.constants import APP_DISPLAY_NAME
from .controllers import CommandController
from .controllers import VerifyController
from .controllers import ScanController
from ui.features.curves.curve_tabs import CurveTabs
from ui.dialogs import select_final_candidate
from ui.dialogs import select_scan_tuning
from ui.dialogs import show_about_dialog
from .error_reporting import ErrorReporter
from ui.features.tuning.gpu_selection import persist_runtime_gpu_index
from ui.features.curves.fan_profiles import sync_profile_fan_payload
from ui.features.tuning.final_choice_controller import handle_final_choice_request
from .models import candidate_id_from_payload
from .models import event_base_points
from .models import event_points
from .models import stage_title
from .models import status_value
from .models import top_status_text
from ui.features.profiles.profiles import load_profile_summaries
from ui.features.profiles.profiles import penguin_burner_runtime_is_active
from ui.features.profiles.profiles import profile_can_apply
from ui.features.profiles.profiles import profile_for_selector
from ui.features.profiles.profiles import runner_status_text
from ui.features.profiles.profiles import running_auto_uv_profile_info
from ui.features.profiles.profiles import systemd_autostart_profile_info
from ui.features.profiles.profiles import systemd_unit_entry_exists
from ui.features.tuning.verify import stop_request_path as verify_stop_request_path
from ui.features.profiles.profile_actions import ProfileActionsMixin
from ui.features.profiles.profile_actions import _manual_curve_control_voltage_mvs
from ui.features.profiles.profile_actions import _runtime_action_label
from .styles import STYLESHEET


class MainWindow(ProfileActionsMixin):
    def __init__(
        self,
        qt_modules,
        *,
        gpu_index: int | None = None,
    ):
        self.QtCore, self.QtGui, self.QtWidgets, self.pg = qt_modules
        self.gpu_index = None if gpu_index is None else max(0, int(gpu_index))
        self.profile_summaries: list[dict] = []
        self.pending_final_result_payload: dict | None = None
        self.final_choice_discarded = False
        self.final_choice_aborted = False
        self._pre_scan_autostart: dict | None = None
        self.last_auto_uv_candidate_id = ""
        self._delete_remove_systemd = False
        self._delete_switch_systemd_profile_id = ""

        self.window = self.QtWidgets.QMainWindow()
        self.window.setWindowTitle(APP_DISPLAY_NAME)
        self.window.resize(1220, 820)
        self._build_ui()
        self.scan_controller = ScanController(
            QtCore=self.QtCore,
            parent=self.window,
            stop_request_path=_stop_request_path(),
        )
        self.scan_controller.on_output = self.log_view.append
        self.scan_controller.on_event = self._handle_scan_event
        self.scan_controller.on_human_line = self._handle_human_line
        self.scan_controller.on_finished = self._scan_finished
        self.command_controller = CommandController(
            QtCore=self.QtCore,
            parent=self.window,
        )
        self.command_controller.on_output = self.log_view.append
        self.command_controller.on_finished = self._command_finished
        self.verify_controller = VerifyController(
            QtCore=self.QtCore,
            parent=self.window,
            stop_request_path=verify_stop_request_path(),
        )
        self.verify_controller.on_output = self.log_view.append
        self.verify_controller.on_progress = self.controls.set_verify_progress
        self.verify_controller.on_finished = self._verify_finished
        self._load_profiles()

    def _build_ui(self) -> None:
        root = self.QtWidgets.QWidget()
        layout = self.QtWidgets.QVBoxLayout(root)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        self.header = StatusHeader(QtCore=self.QtCore, QtWidgets=self.QtWidgets)
        self.controls = ScanControls(QtWidgets=self.QtWidgets)
        self.vf_plot = CurvePlot(
            QtWidgets=self.QtWidgets,
            pg=self.pg,
            x_label="Voltage",
            x_units="mV",
            y_label="Clock",
            y_units="MHz",
        )
        self.runs_table = RunsTable(
            QtCore=self.QtCore,
            QtGui=self.QtGui,
            QtWidgets=self.QtWidgets,
        )
        self.overlay_config = OverlayConfigPanel(
            QtCore=self.QtCore,
            QtWidgets=self.QtWidgets,
        )
        self.runs_table.on_candidate_selection_changed = (
            self.vf_plot.set_highlighted_curve
        )
        self.profile_list = ProfileList(
            QtCore=self.QtCore,
            QtGui=self.QtGui,
            QtWidgets=self.QtWidgets,
        )
        self.log_view = LogView(
            QtCore=self.QtCore,
            QtGui=self.QtGui,
            QtWidgets=self.QtWidgets,
        )

        auto_uv_view = self.QtWidgets.QSplitter(self.QtCore.Qt.Horizontal)
        auto_uv_view.addWidget(self.vf_plot.widget)
        auto_uv_view.addWidget(self.log_view.widget)
        auto_uv_view.setSizes([760, 440])

        self.tabs = self.QtWidgets.QTabWidget()
        self.auto_uv_tab_index = self.tabs.addTab(auto_uv_view, "Auto-UV")
        self.profiles_tab_index = self.tabs.addTab(self.profile_list.widget, "Profiles")
        self.overlay_tab_index = self.tabs.addTab(
            self.overlay_config.widget,
            "Ingame Overlay",
        )
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self._close_dynamic_tab)
        self.errors = ErrorReporter(
            QtCore=self.QtCore,
            QtGui=self.QtGui,
            QtWidgets=self.QtWidgets,
            parent=self.window,
            controls=self.controls,
            log_view=self.log_view,
        )
        self.curve_tabs = CurveTabs(
            QtWidgets=self.QtWidgets,
            pg=self.pg,
            tabs=self.tabs,
            fixed_tab_count=self.tabs.count(),
            show_error=self.errors.show,
        )
        self.afterburner_import = AfterburnerImportWorkflow(
            QtCore=self.QtCore,
            QtGui=self.QtGui,
            QtWidgets=self.QtWidgets,
            pg=self.pg,
            parent=self.window,
            tabs=self.tabs,
            profiles_tab_index=self.profiles_tab_index,
            profile_list=self.profile_list,
            controls=self.controls,
            log_view=self.log_view,
            workflow_running=self._workflow_running,
            load_profiles=self._load_profiles,
            show_error=self.errors.show,
        )

        self.table_panel = self.QtWidgets.QGroupBox("Undervolting runs")
        self.table_panel.setMinimumHeight(220)
        table_layout = self.QtWidgets.QVBoxLayout(self.table_panel)
        table_layout.setContentsMargins(10, 18, 10, 10)
        table_layout.addWidget(self.runs_table.widget)

        layout.addWidget(self.header.widget)
        layout.addWidget(self.controls.widget)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(self.table_panel)

        self.controls.start_button.clicked.connect(self.start_scan)
        self.controls.stop_button.clicked.connect(self.stop_scan)
        self.controls.about_button.clicked.connect(self.show_about)
        self.controls.import_afterburner_button.clicked.connect(self.afterburner_import.run)
        self.profile_list.adaptive_button.clicked.connect(self._run_adaptive_profiles)
        self.profile_list.daemonize_button.clicked.connect(self._run_selected_profile)
        self.profile_list.delete_button.clicked.connect(self._delete_selected_profiles)
        self.profile_list.install_button.toggled.connect(
            self._persist_startup_preference
        )
        self.profile_list.silent_fan_checkbox.toggled.connect(
            self._persist_silent_fan_preference
        )
        self.profile_list.remove_button.clicked.connect(
            lambda: self._run_runtime_action("uninstall-systemd")
        )
        context_menu_policy = getattr(
            getattr(self.QtCore.Qt, "ContextMenuPolicy", self.QtCore.Qt),
            "CustomContextMenu",
        )
        self.profile_list.table.setContextMenuPolicy(context_menu_policy)
        self.profile_list.table.customContextMenuRequested.connect(
            self._show_profile_context_menu
        )
        self.profile_list.set_runtime_actions_enabled(False)
        self.tabs.currentChanged.connect(self._sync_selected_tab_layout)
        self._sync_selected_tab_layout(self.tabs.currentIndex())
        self.window.setCentralWidget(root)
        self.window.setStyleSheet(STYLESHEET)

    def show(self) -> None:
        self.window.show()

    def _sync_selected_tab_layout(self, index: int) -> None:
        # The undervolting-runs table belongs to the Auto-UV workflow only.
        self.table_panel.setVisible(index == self.auto_uv_tab_index)

    def show_about(self) -> None:
        show_about_dialog(
            QtCore=self.QtCore,
            QtGui=self.QtGui,
            QtWidgets=self.QtWidgets,
            parent=self.window,
        )

    def start_scan(self) -> None:
        if self._workflow_running():
            return
        options = select_scan_tuning(
            QtCore=self.QtCore,
            QtGui=self.QtGui,
            QtWidgets=self.QtWidgets,
            parent=self.window,
            gpu_index=self.gpu_index,
        )
        if options is None:
            return
        try:
            self.gpu_index = persist_runtime_gpu_index(options.get("gpu_index", 0))
        except Exception as exc:
            self.errors.show(
                "GPU selection",
                f"Could not save selected GPU index: {exc}",
            )
            return
        options = {**options, "gpu_index": int(self.gpu_index)}
        # Bring the scan into view: the live runs/curve are on the Auto-UV tab.
        self.tabs.setCurrentIndex(self.auto_uv_tab_index)
        command = scan_command(options)
        self.runs_table.clear()
        self.vf_plot.clear()
        self.pending_final_result_payload = None
        self.final_choice_discarded = False
        self.final_choice_aborted = False
        self.last_auto_uv_candidate_id = ""
        # Snapshot the autostart that the scan is about to disable so an aborted
        # run can restore it (profile + silent fan curve + adaptive setting). An
        # empty selector means nothing was autostarting -> nothing to restore.
        autostart_info = systemd_autostart_profile_info()
        self._pre_scan_autostart = (
            dict(autostart_info)
            if str(autostart_info.get("selector", "")).strip()
            else None
        )
        self.controls.hide_dependency_progress()
        self.log_view.append("$ " + " ".join(shlex.quote(part) for part in command) + "\n")
        self.header.set_stage("Starting")
        self.header.set_candidate("Writing to main Auto-UV profile store")
        self.controls.set_running(True)
        self.profile_list.set_runtime_actions_enabled(False)
        if not self.scan_controller.start(command):
            self.controls.set_running(False)
            self._set_profile_actions_enabled(True)

    def stop_scan(self) -> None:
        if self.verify_controller.is_running():
            self.header.set_stage("Stopping")
            self.controls.set_status_text("Stopping profile verification.")
            self.verify_controller.stop()
            return
        if not self.scan_controller.is_running():
            return
        self.header.set_stage("Stopping")
        self.runs_table.mark_running_rows_stopping()
        self.controls.set_status_text("Stopping Auto-UV.")
        self.scan_controller.stop()

    def _handle_scan_event(self, payload: dict) -> None:
        event = str(payload.get("event", ""))
        if event == "auto_uv_start":
            self.header.set_stage("Scanning")
        elif event == "dependency_progress":
            self._handle_dependency_progress(payload)
        elif event == "probe_start":
            self.controls.hide_dependency_progress()
            self.header.set_stage(stage_title(payload.get("stage", "Probe")))
            self.header.set_candidate(_probe_text(payload))
            self.runs_table.add_probe_start(payload)
            self.vf_plot.set_probe_marker(payload)
        elif event == "probe_result":
            self.header.set_stage(stage_title(payload.get("stage", "Probe")))
            self.runs_table.add_probe_result(payload)
            self.vf_plot.set_load_markers(payload)
        elif event == "load_telemetry":
            self.runs_table.update_probe_progress(payload)
            self.vf_plot.set_live_load_marker(payload)
        elif event in {"source_curve", "base_curve"}:
            self.controls.hide_dependency_progress()
            points = event_base_points(payload)
            self.vf_plot.set_source_points(points)
            self.curve_tabs.set_base_points(points)
        elif event == "candidate_curve":
            self.runs_table.record_candidate_curve(payload)
            self.vf_plot.set_candidate_points(
                event_points(payload),
                curve_id=candidate_id_from_payload(payload),
            )
        elif event == "final_choice_request":
            self._handle_final_choice_request(payload)
        elif event == "final_choice_discarded":
            self.final_choice_discarded = True
            self.header.set_stage("Discarded")
            self.header.set_candidate("")
            self.controls.set_status_text(
                "Final verification discarded. No Auto-UV profile was saved."
            )
        elif event == "final_result":
            self.pending_final_result_payload = dict(payload)
            self.last_auto_uv_candidate_id = candidate_id_from_payload(payload)
            self.header.set_stage("Complete")
            self.header.set_candidate(_probe_text(payload))
            self._load_profiles()

    def _handle_human_line(self, line: str) -> None:
        lower = line.lower()
        if "final verification" in lower:
            self.header.set_stage("Final verification")
        elif "candidate" in lower and "mv" in lower:
            self.header.set_stage("Undervolting Candidates Sweep")
            self.header.set_candidate(top_status_text(line))
        elif "auto-uv final state" in lower:
            self.header.set_stage("Complete")
            self.header.set_candidate(top_status_text(line))

    def _handle_dependency_progress(self, payload: dict) -> None:
        detail = str(payload.get("detail") or "Downloading dependencies").strip()
        percent = payload.get("percent", 0)
        self.header.set_stage("Downloading dependencies")
        self.header.set_candidate("")
        self.controls.set_status_text(detail)
        self.controls.set_dependency_progress(percent, detail=detail)

    def _handle_final_choice_request(self, payload: dict) -> None:
        result = handle_final_choice_request(
            payload,
            QtCore=self.QtCore,
            QtGui=self.QtGui,
            QtWidgets=self.QtWidgets,
            parent=self.window,
            scan_controller=self.scan_controller,
            log_view=self.log_view,
            select_final_candidate_fn=select_final_candidate,
        )
        if result.discarded:
            self.final_choice_discarded = True
        if result.aborted:
            self.final_choice_aborted = True
        if result.selected_candidate_id:
            self.last_auto_uv_candidate_id = result.selected_candidate_id

    def _scan_finished(self, exit_code, exit_status, stopped_by_user: bool) -> None:
        status_name = "finished" if int(exit_code) == 0 else "stopped"
        self.log_view.append(f"\nAuto-UV process {status_name}: exit_code={exit_code}\n")
        failed = int(exit_code) != 0 and not stopped_by_user
        if self.final_choice_aborted:
            # A user-requested abort is not a failure even though the scan
            # process exits non-zero: label the run "Aborted", never "Failed".
            self.header.set_stage("Aborted")
            self.runs_table.mark_running_rows_stopped(
                label="Aborted", row_state="warning"
            )
            self.controls.set_status_text("Auto-UV aborted by user.")
        elif failed:
            self.header.set_stage("Error")
            self.runs_table.mark_running_rows_stopped(label="Failed")
            self.controls.set_status_text("Auto-UV failed.")
            self.errors.show_process(
                title="Auto-UV failed",
                action_label="Auto-UV scan",
                exit_code=exit_code,
                exit_status=exit_status,
            )
        elif self.final_choice_discarded:
            self.header.set_stage("Discarded")
            self.runs_table.mark_running_rows_stopped(label="Discarded")
        elif self.pending_final_result_payload is not None:
            self.header.set_stage("Complete")
            self.controls.set_status_text("Final verification complete.")
            self.tabs.setCurrentIndex(self.profiles_tab_index)
        elif stopped_by_user:
            self.header.set_stage("Stopped")
            self.runs_table.mark_running_rows_stopped(label="Stopped")
        else:
            self.header.set_stage("Idle")
        self.controls.set_running(False)
        self.controls.hide_dependency_progress()
        self.profile_list.set_runtime_actions_enabled(False)
        was_aborted = self.final_choice_aborted
        self.pending_final_result_payload = None
        self.final_choice_discarded = False
        self.final_choice_aborted = False
        self._load_profiles()
        if was_aborted:
            self._restore_pre_scan_autostart()

    def _restore_pre_scan_autostart(self) -> None:
        # On abort, bring back the autostart profile the scan disabled, including
        # its silent fan curve and adaptive setting. If nothing was autostarting
        # before the scan there is nothing to restore.
        snapshot = self._pre_scan_autostart
        self._pre_scan_autostart = None
        if not snapshot:
            return
        selector = str(snapshot.get("selector", "")).strip()
        if not selector:
            return
        adaptive = bool(snapshot.get("adaptive_auto_uv"))
        silent_fan = bool(snapshot.get("silent_fan_curve"))
        # "__systemd_default__" means the unit pinned no explicit profile (it ran
        # the latest profile); restore that by passing an empty selector so the
        # CLI falls back to its default again.
        restore_selector = "" if selector == "__systemd_default__" else selector
        if silent_fan and not adaptive:
            profile = profile_for_selector(self.profile_summaries, selector)
            if profile:
                # The daemon needs the fan payload on disk before it starts.
                sync_profile_fan_payload(profile)
        command = runtime_profile_command(
            "install-systemd",
            profile_selector=restore_selector,
            silent_fan_curve=silent_fan,
            adaptive_auto_uv=adaptive,
            gpu_index=self.gpu_index,
        )
        self._persist_silent_fan_preference(silent_fan)
        self._persist_startup_preference(True)
        self.log_view.append(
            "\nRestoring the previous autostart profile (incl. fan curve) after abort.\n"
        )
        self._set_profile_actions_enabled(False)
        self.command_controller.start(
            "adaptive-install-systemd" if adaptive else "install-systemd",
            command,
            fail_text="Failed to restore the previous autostart profile.",
        )

    def _close_dynamic_tab(self, index: int) -> None:
        self.curve_tabs.close_tab(index)

    def _verify_finished(self, exit_code, exit_status, stopped_by_user: bool) -> None:
        success = int(exit_code) == 0
        self.log_view.append(f"\nProfile verification finished: exit_code={exit_code}\n")
        if success:
            target = int(self.verify_controller.target_duration_s or 0)
            self.controls.set_verify_progress(
                100,
                elapsed_s=target,
                target_s=target,
                detail="Profile verification complete.",
            )
            self.controls.set_status_text("Profile verification complete.")
            self.header.set_stage("Idle")
            self.QtCore.QTimer.singleShot(2500, self.controls.hide_dependency_progress)
        else:
            self.controls.hide_dependency_progress()
            self.controls.set_status_text(
                "Profile verification stopped."
                if stopped_by_user
                else "Profile verification failed."
            )
            self.header.set_stage("Idle" if stopped_by_user else "Error")
            if not stopped_by_user:
                self.errors.show_process(
                    title="Profile verification failed",
                    action_label="Profile verification",
                    exit_code=exit_code,
                    exit_status=exit_status,
                )
        self.controls.set_running(False)
        self._load_profiles()

    def _load_profiles(self) -> None:
        self.profile_summaries = load_profile_summaries()
        autostart_info = systemd_autostart_profile_info()
        has_systemd_entry = systemd_unit_entry_exists()
        running_info = (
            running_auto_uv_profile_info()
            if penguin_burner_runtime_is_active()
            else {"selector": "", "silent_fan_curve": False, "adaptive_auto_uv": False}
        )
        systemd_selector = str(autostart_info["selector"])
        if systemd_selector in {"active", "latest", "__systemd_default__"}:
            systemd_selector = str(
                (self.profile_summaries[0] if self.profile_summaries else {}).get(
                    "profile_id",
                    "",
                )
            )
        self.profile_list.set_profiles(
            self.profile_summaries,
            systemd_selector=systemd_selector,
            has_systemd_entry=has_systemd_entry,
            persist_on_startup_checked=persist_on_startup_from_runtime_config(
                default=has_systemd_entry,
            ),
            preferred_candidate_id=self.last_auto_uv_candidate_id,
            select_preferred=bool(self.last_auto_uv_candidate_id),
            # The silent-fan tick is sticky: the user's persisted choice is
            # authoritative and survives discarded/aborted Auto-UV runs and
            # reloads. We OR in the live runtime / autostart flag only so an
            # already-applied silent-fan profile still shows checked on a fresh
            # install where nothing has been toggled yet.
            silent_fan_checked=(
                silent_fan_curve_from_runtime_config()
                or bool(running_info["silent_fan_curve"])
                or bool(autostart_info["silent_fan_curve"])
            ),
        )
        self._set_profile_actions_enabled(not self._workflow_running())
        self.controls.set_status_text(
            runner_status_text(
                self.profile_summaries,
                running_selector=str(running_info["selector"]),
                autostart_selector=str(autostart_info["selector"]),
                running_silent_fan=bool(running_info["silent_fan_curve"]),
                autostart_silent_fan=bool(autostart_info["silent_fan_curve"]),
            )
        )

    def _persist_startup_preference(self, checked: bool) -> None:
        persist_on_startup_to_runtime_config(bool(checked))

    def _persist_silent_fan_preference(self, checked: bool) -> None:
        # Remember the silent-fan choice durably so the "latest profile setup"
        # restores it after an aborted Auto-UV run, profile reload, or restart.
        silent_fan_curve_to_runtime_config(bool(checked))

    def _workflow_running(self) -> bool:
        return (
            self.scan_controller.is_running()
            or self.command_controller.is_running()
            or self.verify_controller.is_running()
        )

    def _set_profile_actions_enabled(self, enabled: bool) -> None:
        self.profile_list.set_runtime_actions_enabled(bool(enabled))

def _probe_text(payload: dict) -> str:
    voltage = status_value(payload.get("voltage_mv") or payload.get("candidate_voltage_mv"))
    clock = status_value(payload.get("clock_mhz") or payload.get("lock_clock_mhz"))
    return f"{voltage or 'n/a'} mV @ {clock or 'n/a'} MHz"


def _stop_request_path() -> Path:
    return default_user_config_dir() / "auto-uv-stop-requested"
