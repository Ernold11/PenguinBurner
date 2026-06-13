"""Shared PenguinBurner runtime exceptions.

Small modules raise these errors so the CLI can present one consistent failure path.
"""


class NvmlError(RuntimeError):
    pass


class FanCurveBlockedError(NvmlError):
    pass

