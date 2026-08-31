from __future__ import annotations

import pytest

from pipeline.encoder import reset_encoder_cache


@pytest.fixture(autouse=True)
def _reset_encoder_cache() -> object:
    reset_encoder_cache()
    yield
    reset_encoder_cache()
