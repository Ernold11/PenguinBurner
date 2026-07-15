"""Mobile GPU identity gate for the fixed power-limit control.

Some Nvidia notebook boards expose readable power-limit fields while
rejecting the manual setter; the GUI grays the power-limit control out for
them by identity. All GPU writes themselves run in the Rust daemon
(``burnerd``), whose live setter probe is the runtime backstop for mobile
parts this identity check does not recognize.
"""

from __future__ import annotations

FIXED_POWER_LIMIT_MOBILE_NAME_TOKENS = (
    "laptop",
    "mobile",
    "notebook",
    "max-q",
    "max q",
)
FIXED_POWER_LIMIT_MOBILE_PCI_DEVICE_IDS = frozenset(
    {
        # Blackwell notebook IDs from NVIDIA supported-chip tables.
        "2BB4",
        "2C18",
        "2C19",
        "2C38",
        "2C39",
        "2C58",
        "2C59",
        "2D18",
        "2D19",
        "2D39",
        "2D58",
        "2D59",
        "2DB8",
        "2DB9",
        "2F18",
        "2F38",
        "2F58",
    }
)

__all__ = [
    "fixed_power_limit_excluded_by_identity",
]


def fixed_power_limit_excluded_by_identity(
    *,
    gpu_name: object | None = None,
    pci_device_id: object | None = None,
) -> bool:
    name = str(gpu_name or "").lower()
    if any(token in name for token in FIXED_POWER_LIMIT_MOBILE_NAME_TOKENS):
        return True
    return _normalize_pci_device_id(pci_device_id) in (
        FIXED_POWER_LIMIT_MOBILE_PCI_DEVICE_IDS
    )


def _normalize_pci_device_id(value: object | None) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    text = (
        text.replace("0X", "")
        .replace(":", " ")
        .replace("-", " ")
        .replace("_", " ")
    )
    parts = [
        "".join(ch for ch in part if ch in "0123456789ABCDEF")
        for part in text.split()
    ]
    parts = [part for part in parts if part]
    if len(parts) >= 2 and parts[0] == "10DE":
        return parts[1][-4:].zfill(4)
    token = parts[0] if parts else ""
    if len(token) >= 8 and token.endswith("10DE"):
        return token[:4]
    if len(token) >= 8 and token.startswith("10DE"):
        return token[4:8]
    return token[-4:].zfill(4) if token else ""
