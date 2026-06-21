from __future__ import annotations

import re




def pci_bus_number_from_bus_id(pci_bus_id: str) -> int | None:
    text = str(pci_bus_id or "").strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) >= 2:
        bus_text = parts[-2]
    else:
        match = re.search(r"(^|[^0-9a-fA-F])([0-9a-fA-F]{2})(?=[:._-])", text)
        bus_text = match.group(2) if match else ""
    try:
        return int(bus_text, 16)
    except ValueError:
        return None
