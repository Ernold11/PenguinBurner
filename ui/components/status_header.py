from __future__ import annotations


class StatusHeader:
    def __init__(self, *, QtCore, QtWidgets):
        self.QtCore = QtCore
        self.QtWidgets = QtWidgets
        self.widget = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(self.widget)
        layout.setContentsMargins(0, 0, 0, 0)
        self.stage_label = QtWidgets.QLabel("Idle")
        self.stage_label.setObjectName("stageLabel")
        self.candidate_label = QtWidgets.QLabel("No active scan")
        self.candidate_label.setObjectName("candidateLabel")
        self.candidate_label.setWordWrap(True)
        self.candidate_label.setMinimumWidth(0)
        self.candidate_label.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )
        self.candidate_label.setTextInteractionFlags(
            _text_interaction_flags(
                QtCore,
                "TextSelectableByMouse",
                "TextSelectableByKeyboard",
            )
        )
        context_menu_policy = getattr(
            getattr(QtCore.Qt, "ContextMenuPolicy", QtCore.Qt),
            "CustomContextMenu",
        )
        self.candidate_label.setContextMenuPolicy(context_menu_policy)
        self.candidate_label.customContextMenuRequested.connect(
            self._show_candidate_context_menu
        )
        layout.addWidget(self.stage_label)
        layout.addWidget(self.candidate_label, 1)

    def set_stage(self, text: str) -> None:
        self.stage_label.setText(str(text))

    def stage(self) -> str:
        return self.stage_label.text()

    def set_candidate(self, text: str) -> None:
        self.candidate_label.setText(str(text))

    def _show_candidate_context_menu(self, position) -> None:
        text = str(self.candidate_label.text())
        if not text:
            return
        menu = self.QtWidgets.QMenu(self.candidate_label)
        copy_action = menu.addAction("Copy")
        chosen = menu.exec(self.candidate_label.mapToGlobal(position))
        if chosen == copy_action:
            clipboard = self.QtWidgets.QApplication.clipboard()
            if clipboard is not None:
                clipboard.setText(text)


def _text_interaction_flags(QtCore, *names: str):
    enum = getattr(QtCore.Qt, "TextInteractionFlag", QtCore.Qt)
    value = None
    for name in names:
        flag = getattr(enum, name)
        value = flag if value is None else value | flag
    return value
