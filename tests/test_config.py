from pathlib import Path

from pipeline.broll import BrollKind, BrollSpec
from pipeline.config import Settings, load_settings
from pipeline.layouts import PictureTag


def test_default_canvas_and_pip() -> None:
    settings = Settings()
    assert settings.output_width == 1920
    assert settings.output_height == 1080
    assert settings.bookend_seconds == 10
    assert settings.host_name == "Scott Mastin"
    assert settings.encode_concurrency == 2
    assert settings.silence_min_duration == 1.0
    assert settings.silence_padding == 0.30
    assert settings.silence_threshold_db == -45.0


def test_layout_values() -> None:
    assert PictureTag.OVERLAY.value == "overlay"
    assert PictureTag.PIP.value == "pip"
    assert PictureTag.NOTHING.value == "nothing"
    assert PictureTag.LOWER_THIRD.value == "lower_third"


def test_broll_spec_defaults_to_slide() -> None:
    spec = BrollSpec(title="Hook")
    assert spec.kind is BrollKind.SLIDE


def test_director_chunk_defaults() -> None:
    settings = Settings()
    assert settings.director_chunk_seconds == 300
    assert settings.director_chunk_threshold == 480


def test_settings_default_gemini_model() -> None:
    settings = Settings()
    assert settings.gemini_model == "gemini-3.6-flash"


def test_load_settings_reads_env_file(tmp_path: Path | None = None) -> None:
    env = Path("/tmp/yt-pipe-settings.env")
    env.write_text("GEMINI_API_KEY=test-key\nOUTPUT_WIDTH=1280\n", encoding="utf-8")
    settings = load_settings(env)
    assert settings.gemini_api_key == "test-key"
    assert settings.output_width == 1280


def test_load_settings_defaults_gemini_model(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    env = tmp_path / ".env"
    env.write_text("GEMINI_API_KEY=test-key\n", encoding="utf-8")
    settings = load_settings(env)
    assert settings.gemini_model == "gemini-3.6-flash"


def test_load_settings_respects_gemini_model_override(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GEMINI_MODEL", "gemini-custom-pin")
    env = tmp_path / ".env"
    env.write_text("GEMINI_API_KEY=test-key\n", encoding="utf-8")
    settings = load_settings(env)
    assert settings.gemini_model == "gemini-custom-pin"


def test_load_settings_blank_gemini_model_falls_back(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GEMINI_MODEL", "   ")
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    settings = load_settings(env)
    assert settings.gemini_model == "gemini-3.6-flash"
