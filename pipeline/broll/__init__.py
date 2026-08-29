"""B-roll providers.

Slides are the default stills. Local video files in --broll-dir resolve to the
same BrollAsset shape. No stock-footage APIs.
"""

from pipeline.broll.base import (
    BrollAsset,
    BrollKind,
    BrollProvider,
    BrollSpec,
    SlideVariant,
)
from pipeline.broll.local import (
    LocalVideoProvider,
    apply_local_broll,
    match_local_broll,
)
from pipeline.broll.slides import (
    PlaywrightNotFoundError,
    SlideProvider,
    collect_slide_jobs,
    render_slides,
)

__all__ = [
    "BrollAsset",
    "BrollKind",
    "BrollProvider",
    "BrollSpec",
    "LocalVideoProvider",
    "PlaywrightNotFoundError",
    "SlideProvider",
    "SlideVariant",
    "apply_local_broll",
    "collect_slide_jobs",
    "match_local_broll",
    "render_slides",
]
