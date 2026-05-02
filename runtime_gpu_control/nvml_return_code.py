"""NVML constants and return-code checks.

Every direct NVML call uses the same small guard so failures produce one error type.
"""

from __future__ import annotations

from penguin_burner_errors import NvmlError


NVML_SUCCESS = 0
NVML_TEMPERATURE_GPU = 0
NVML_CLOCK_GRAPHICS = 0
NVML_CLOCK_MEM = 2


def check_nvml_return_code(rc, name):
    if rc != NVML_SUCCESS:
        raise NvmlError(f"{name} failed with NVML error {rc}")
