"""Local YouTube editing pipeline stages.

Each stage is independently replaceable: silence trim, Gemini director,
and MoviePy compositor share only the pydantic edit-script models.
"""

from pipeline.config import Settings, load_settings, require_ffmpeg
from pipeline.layouts import LayoutKind, PictureTag
from pipeline.models import EditScript, SilenceCutMap, SilenceTrimResult
from pipeline.pacing import enforce_pacing, evaluate_pacing
from pipeline.shotlist import resolve_edit_script

__all__ = [
    "EditScript",
    "LayoutKind",
    "PictureTag",
    "Settings",
    "SilenceCutMap",
    "SilenceTrimResult",
    "enforce_pacing",
    "evaluate_pacing",
    "load_settings",
    "require_ffmpeg",
    "resolve_edit_script",
]
