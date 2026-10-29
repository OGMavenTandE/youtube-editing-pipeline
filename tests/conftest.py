"""Root fixtures for the older pipeline package.

``pytest tests/ytp`` must collect even when that package (and dotenv) is not
installed. The cache reset runs only when the package imports.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_encoder_cache() -> object:
    try:
        from pipeline.encoder import reset_encoder_cache
        from pipeline.hwaccel import reset_hwaccel_cache
    except ImportError:
        yield
        return
    reset_encoder_cache()
    reset_hwaccel_cache()
    yield
    reset_encoder_cache()
    reset_hwaccel_cache()
