from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field


class BrollKind(str, Enum):
    SLIDE = "slide"
    VIDEO = "video"


class BrollSpec(BaseModel):
    """Provider-agnostic request for one visual under a scene."""

    kind: BrollKind = BrollKind.SLIDE
    title: str = ""
    bullets: list[str] = Field(default_factory=list)
    query: str = ""
    asset_path: Path | None = None
    duration: float = Field(default=0.0, ge=0.0)


class BrollAsset(BaseModel):
    """Rendered or resolved media ready for the compositor."""

    kind: BrollKind
    path: Path
    duration: float | None = None


class BrollProvider(Protocol):
    def render(self, spec: BrollSpec) -> BrollAsset:
        """Produce a still or clip. Video providers can land here later."""
        ...
