"""Read values from raw probe records without caring whether they are dicts or objects.

This prevents Q2RTX, CUDA, and telemetry rule modules from copying field-access glue.
"""

from __future__ import annotations

from typing import Any


def read_field(record: Any, name: str) -> Any:
    if isinstance(record, dict):
        return record.get(name)
    return getattr(record, name, None)


def numeric_values(records: list[Any], field_name: str) -> list[float]:
    values = []
    for record in records:
        value = read_field(record, field_name)
        if value is None:
            continue
        values.append(float(value))
    return values


def percent(value: float | int) -> float:
    return max(0.0, float(value) / 100.0)

