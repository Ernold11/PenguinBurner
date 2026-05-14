"""Final verification ports the chosen Auto-UV curve from short probes to a saved profile.

The package keeps long-probe execution, clock recovery, artifacts, and fan suggestions separate.
"""

from .main_loop import run_final_verification_and_save

__all__ = ["run_final_verification_and_save"]
