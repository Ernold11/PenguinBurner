from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .constants import DEFAULT_FINAL_VERIFICATION_DURATION_S
from .constants import MAX_FINAL_VERIFICATION_DURATION_S


@dataclass(slots=True)
class FinalChoiceRequestResult:
    discarded: bool = False
    aborted: bool = False
    selected_candidate_id: str = ""


def handle_final_choice_request(
    payload: dict,
    *,
    QtCore,
    QtGui,
    QtWidgets,
    parent,
    scan_controller,
    log_view,
    select_final_candidate_fn: Callable,
) -> FinalChoiceRequestResult:
    candidates = [
        dict(candidate)
        for candidate in payload.get("candidates", [])
        if isinstance(candidate, dict)
    ]
    auto_uv_mode = str(payload.get("auto_uv_mode", "")).strip()
    default_id = str(payload.get("default_candidate_id", "")).strip()
    default_duration_s = duration_seconds(
        payload.get("final_verification_duration_s"),
        DEFAULT_FINAL_VERIFICATION_DURATION_S,
    )
    max_duration_s = duration_seconds(
        payload.get("max_final_verification_duration_s"),
        MAX_FINAL_VERIFICATION_DURATION_S,
    )
    request_reason = str(payload.get("request_reason", "")).strip()
    recovery_decision = payload.get("recovery_decision")
    if not isinstance(recovery_decision, dict):
        recovery_decision = None
    selected, duration_s, action = select_final_candidate_fn(
        QtCore=QtCore,
        QtGui=QtGui,
        QtWidgets=QtWidgets,
        parent=parent,
        candidates=candidates,
        default_candidate_id=default_id,
        default_duration_s=default_duration_s,
        max_duration_s=max_duration_s,
        auto_uv_mode=auto_uv_mode,
        request_reason=request_reason,
        recovery_decision=recovery_decision,
    )
    if action is True:
        action = "discard"
    elif action is False:
        action = "select"
    action = str(action or "select").strip().lower()
    response_path = str(payload.get("response_path", "")).strip()
    if not response_path:
        return FinalChoiceRequestResult()
    if action == "abort":
        scan_controller.write_json_response(response_path, {"action": "abort"})
        log_view.append("Auto-UV recovery aborted by user.\n")
        return FinalChoiceRequestResult(discarded=True, aborted=True)
    if action == "discard":
        scan_controller.write_json_response(response_path, {"action": "discard"})
        if request_reason == "previous-crash":
            log_view.append("Starting a new Auto-UV scan from scratch.\n")
            return FinalChoiceRequestResult()
        log_view.append("Final verification discarded by user.\n")
        return FinalChoiceRequestResult(discarded=True)
    selected_id = (
        str(selected.get("candidate_id", "")).strip()
        if selected is not None
        else default_id
    )
    scan_controller.write_json_response(
        response_path,
        {
            "candidate_id": selected_id,
            "final_verification_duration_s": int(duration_s),
        },
    )
    log_view.append(
        "Selected Final verification candidate: "
        f"{selected_id}; duration: {int(duration_s)}s\n"
    )
    return FinalChoiceRequestResult(selected_candidate_id=selected_id)


def duration_seconds(value, default_s: int) -> int:
    try:
        return max(1, int(round(float(value))))
    except (TypeError, ValueError):
        return int(default_s)
