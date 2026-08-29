from pathlib import Path

from pipeline.broll import BrollKind, BrollSpec
from pipeline.config import Settings, load_settings
from pipeline.layouts import LayoutKind


def test_default_canvas_and_pip() -> None:
    settings = Settings()
    assert settings.output_width == 1920
    assert settings.output_height == 1080
    assert settings.pip_scale == 0.25
    assert abs(settings.split_top_ratio - 2.0 / 3.0) < 1e-9


def test_layout_values() -> None:
    assert LayoutKind.FULL_FRAME.value == "FULL_FRAME"
    assert LayoutKind.PIP_BOTTOM_RIGHT.value == "PIP_BOTTOM_RIGHT"
    assert LayoutKind.SPLIT_TOP.value == "SPLIT_TOP"


def test_broll_spec_defaults_to_slide() -> None:
    spec = BrollSpec(title="Hook")
    assert spec.kind is BrollKind.SLIDE


def test_director_chunk_defaults() -> None:
    settings = Settings()
    assert settings.director_chunk_seconds == 300
    assert settings.director_chunk_threshold == 480


def test_load_settings_reads_env_file(tmp_path: Path | None = None) -> None:
    env = Path("/tmp/yt-pipe-settings.env")
    env.write_text("GEMINI_API_KEY=test-key\nOUTPUT_WIDTH=1280\n", encoding="utf-8")
    settings = load_settings(env)
    assert settings.gemini_api_key == "test-key"
    assert settings.output_width == 1280
