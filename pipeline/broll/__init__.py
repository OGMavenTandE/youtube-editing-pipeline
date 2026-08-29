"""B-roll providers.

Slides are the current implementation (Task 4). Video clips can be added later
as another provider that returns the same BrollAsset shape.
"""

from pipeline.broll.base import BrollAsset, BrollKind, BrollProvider, BrollSpec

__all__ = ["BrollAsset", "BrollKind", "BrollProvider", "BrollSpec"]
