"""Performance-mode Auto-OC candidate search."""

from .ladder import AutoOcStep, build_auto_oc_ladder
from .scoring import auto_oc_probe_key, effective_q2rtx_clock_mhz
from .search import AutoOcSearchResult, run_auto_oc_candidate_search
from .settings import (
    AUTO_OC_DEFAULT_MAX_INTERPOLATION_STEPS,
    AUTO_OC_TARGET_PROFILE_ID,
)

__all__ = [
    "AUTO_OC_DEFAULT_MAX_INTERPOLATION_STEPS",
    "AUTO_OC_TARGET_PROFILE_ID",
    "AutoOcSearchResult",
    "AutoOcStep",
    "auto_oc_probe_key",
    "build_auto_oc_ladder",
    "effective_q2rtx_clock_mhz",
    "run_auto_oc_candidate_search",
]
