"""Local YouTube editing pipeline stages.

Each stage is independently replaceable: silence trim, Gemini director,
and MoviePy compositor share only the pydantic edit-script models.
"""

from pipeline.config import Settings, load_settings, require_ffmpeg
from pipeline.models import EditScript, SilenceCutMap, SilenceTrimResult

__all__ = [
    "EditScript",
    "Settings",
    "SilenceCutMap",
    "SilenceTrimResult",
    "load_settings",
    "require_ffmpeg",
]
