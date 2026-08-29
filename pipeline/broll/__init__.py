"""B-roll providers.

Slides are the current implementation (Task 4). Video clips can be added later
as another provider that returns the same BrollAsset shape.
"""

from pipeline.broll.base import (
    BrollAsset,
    BrollKind,
    BrollProvider,
    BrollSpec,
    SlideVariant,
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
    "PlaywrightNotFoundError",
    "SlideProvider",
    "SlideVariant",
    "collect_slide_jobs",
    "render_slides",
]
