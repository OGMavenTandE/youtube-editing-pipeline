from __future__ import annotations

import pytest

from pipeline.encoder import reset_encoder_cache
from pipeline.hwaccel import reset_hwaccel_cache


@pytest.fixture(autouse=True)
def _reset_encoder_cache() -> object:
    reset_encoder_cache()
    reset_hwaccel_cache()
    yield
    reset_encoder_cache()
    reset_hwaccel_cache()
