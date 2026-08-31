from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from pipeline.layouts import PictureTag


class BrollKind(str, Enum):
    SLIDE = "slide"
    VIDEO = "video"


class SlideVariant(str, Enum):
    PIP_LIST = "pip_list"
    PIP_CLAIM = "pip_claim"
    SPLIT = "split"
    LOWER_THIRD = "lower_third"


class BrollSpec(BaseModel):
    """Provider-agnostic request for one visual under a scene."""

    kind: BrollKind = BrollKind.SLIDE
    kicker: str = ""
    title: str = ""
    bullets: list[str] = Field(default_factory=list)
    query: str = ""
    asset_path: Path | None = None
    duration: float = Field(default=0.0, ge=0.0)
    layout: PictureTag = PictureTag.NOTHING
    slide_id: str = ""
    lower_third_title: str = ""
    lower_third_subtitle: str = ""
    variant: SlideVariant = SlideVariant.PIP_LIST


class BrollAsset(BaseModel):
    """Rendered or resolved media ready for the compositor."""

    kind: BrollKind
    path: Path
    duration: float | None = None


class BrollProvider(Protocol):
    def render(self, spec: BrollSpec) -> BrollAsset:
        """Produce a still or clip. Video providers can land here later."""
        ...
