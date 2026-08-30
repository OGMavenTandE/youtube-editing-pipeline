from __future__ import annotations

from pathlib import Path

from desktop.config_store import AppConfig, load_config, save_config
from desktop.envfile import read_env_value, upsert_env_value
from desktop.logutil import sanitize_log_line
from desktop.oauth import OAuthConfigError, parse_client_secret_json
from desktop.paths import client_secret_candidates
from pipeline.config import default_env_file


def test_upsert_env_replaces_and_preserves(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("# comment\nGEMINI_API_KEY=old\nGEMINI_MODEL=flash\n", encoding="utf-8")
    upsert_env_value(path, "GEMINI_API_KEY", "new-key")
    text = path.read_text(encoding="utf-8")
    assert "GEMINI_API_KEY=new-key" in text
    assert "old" not in text
    assert "GEMINI_MODEL=flash" in text
    assert "# comment" in text
    assert read_env_value(path, "GEMINI_API_KEY") == "new-key"


def test_upsert_env_uncomments_placeholder(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("# GOOGLE_OAUTH_CLIENT_ID=\n", encoding="utf-8")
    upsert_env_value(path, "GOOGLE_OAUTH_CLIENT_ID", "abc.apps.googleusercontent.com")
    assert read_env_value(path, "GOOGLE_OAUTH_CLIENT_ID") == "abc.apps.googleusercontent.com"
    assert "# GOOGLE_OAUTH_CLIENT_ID" not in path.read_text(encoding="utf-8")


def test_parse_desktop_client_secret(tmp_path: Path) -> None:
    path = tmp_path / "client_secret.json"
    path.write_text(
        '{"installed": {"client_id": "id.apps.googleusercontent.com", "client_secret": "s"}}',
        encoding="utf-8",
    )
    assert parse_client_secret_json(path) == ("id.apps.googleusercontent.com", "s")


def test_parse_web_client_secret_rejected(tmp_path: Path) -> None:
    path = tmp_path / "web.json"
    path.write_text('{"web": {"client_id": "id", "client_secret": "s"}}', encoding="utf-8")
    try:
        parse_client_secret_json(path)
    except OAuthConfigError as exc:
        assert "Desktop" in str(exc) or "desktop" in str(exc)
    else:
        raise AssertionError("web client should be rejected")


def test_sanitize_log_hides_key() -> None:
    assert sanitize_log_line("GEMINI_API_KEY=secret-value") == "[redacted]"
    assert sanitize_log_line("Downloading talk.mp4") == "Downloading talk.mp4"


def test_config_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "app_config.json"
    saved = save_config(
        AppConfig(inbox_folder_id="in", outbox_folder_id="out", done_folder_id="done"),
        path,
    )
    loaded = load_config(saved)
    assert loaded.folders_ready()
    assert loaded.inbox_folder_id == "in"


def test_client_secret_candidates_include_desktop() -> None:
    paths = [str(path) for path in client_secret_candidates()]
    assert any(path.endswith("desktop/client_secret.json") for path in paths)


def test_desktop_package_relative_imports() -> None:
    import ast

    from desktop.config_store import AppConfig
    from desktop.startup import set_startup

    assert AppConfig().wizard_complete is False
    assert callable(set_startup)
    worker = Path(__file__).resolve().parent.parent / "desktop" / "worker.py"
    ast.parse(worker.read_text(encoding="utf-8"))


def test_spec_collects_desktop_and_pipeline_submodules() -> None:
    spec = Path(__file__).resolve().parent.parent / "desktop" / "youtube-pipeline.spec"
    text = spec.read_text(encoding="utf-8")
    assert "collect_submodules(\"desktop\")" in text
    assert "collect_submodules(\"pipeline\")" in text
    assert "collect_all(\"desktop\")" in text
    assert "pyi_rth_syspath.py" in text
    assert 'ROOT / "desktop" / "__main__.py"' in text
    assert 'ROOT / "desktop" / "app.py"' not in text


def test_default_env_file_override(monkeypatch, tmp_path: Path) -> None:
    custom = tmp_path / "custom.env"
    monkeypatch.setenv("YOUTUBE_PIPELINE_ENV", str(custom))
    assert default_env_file() == custom
