from __future__ import annotations

from pathlib import Path

from desktop.config_store import AppConfig, load_config, save_config
from desktop.envfile import read_env_value, upsert_env_value
from desktop.logutil import sanitize_log_line
from desktop.oauth import OAuthConfigError, parse_client_secret_json
from desktop.paths import client_secret_candidates, env_path, migrate_install_env
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


def test_job_failure_logs_traceback(monkeypatch, tmp_path: Path) -> None:
    from desktop.worker import PipelineWorker
    from pipeline.config import Settings

    logs: list[str] = []
    worker = PipelineWorker(log=logs.append, status=lambda *_: None)
    monkeypatch.setattr(
        "desktop.worker.pipeline_settings",
        lambda: Settings(
            input_dir=tmp_path / "input",
            output_dir=tmp_path / "output",
            work_dir=tmp_path / "work",
            slides_dir=tmp_path / "slides",
            scenes_dir=tmp_path / "scenes",
        ),
    )

    class Store:
        def claim(self, *args: object, **kwargs: object) -> bool:
            return True

        def mark_error(self, file_id: str, message: str) -> None:
            return None

    class Client:
        def claim_file(self, file_id: str) -> bool:
            return True

        def download_resumable(self, *args: object, **kwargs: object) -> None:
            raise AttributeError("'NoneType' object has no attribute 'stdout'")

    class Item:
        id = "abc"
        name = "talk.mp4"

    worker._process(Client(), Store(), AppConfig(), Item(), "talk")
    text = "\n".join(logs)
    assert any(line.startswith("Job failed:") for line in logs)
    assert "Traceback (most recent call last):" in text
    assert "AttributeError" in text
    assert "stdout" in text


def test_sanitize_log_hides_key() -> None:
    assert sanitize_log_line("GEMINI_API_KEY=secret-value") == "[redacted]"
    assert sanitize_log_line("Downloading talk.mp4") == "Downloading talk.mp4"


def test_sanitize_log_keeps_missing_key_error() -> None:
    line = "Gemini API key is not set. Open Settings and paste a Google AI Studio key."
    assert sanitize_log_line(line) == line
    named = "GEMINI_API_KEY is not set. Copy .env.example to .env"
    assert sanitize_log_line(named) == named


def test_sanitize_log_keeps_key_names_without_values() -> None:
    secret_file = "Could not find client_secret.json next to the EXE."
    assert sanitize_log_line(secret_file) == secret_file
    token_name = "Failed to refresh access_token from disk."
    assert sanitize_log_line(token_name) == token_name


def test_sanitize_log_redacts_secret_values() -> None:
    aiza = "Using key AIzaSyA-test-key-value-1234567890abcd"
    cleaned = sanitize_log_line(aiza)
    assert "AIza" not in cleaned
    assert "[redacted]" in cleaned
    assigned = sanitize_log_line("access_token=ya29.a0AfH6SMB-very-long-token-value-here")
    assert "ya29" not in assigned
    assert assigned == "[redacted]"
    jsonish = sanitize_log_line('{"refresh_token": "1//0eVeryLongRefreshTokenValueHere"}')
    assert "1//" not in jsonish
    assert "[redacted]" in jsonish
    long_token = "token " + ("AbCdEfGh" * 8)
    assert "AbCdEfGh" not in sanitize_log_line(long_token)


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
    assert ".local-browsers" in text
    assert "playwright/driver/package/.local-browsers" in text


def test_default_env_file_override(monkeypatch, tmp_path: Path) -> None:
    custom = tmp_path / "custom.env"
    monkeypatch.setenv("YOUTUBE_PIPELINE_ENV", str(custom))
    assert default_env_file() == custom


def test_env_path_override_wins(monkeypatch, tmp_path: Path) -> None:
    custom = tmp_path / "custom.env"
    monkeypatch.setenv("YOUTUBE_PIPELINE_ENV", str(custom))
    monkeypatch.setattr("desktop.paths.is_frozen", lambda: True)
    assert env_path() == custom


def test_env_path_source_checkout_uses_repo_root(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("YOUTUBE_PIPELINE_ENV", raising=False)
    monkeypatch.setattr("desktop.paths.is_frozen", lambda: False)
    monkeypatch.setattr("desktop.paths.install_root", lambda: tmp_path)
    assert env_path() == tmp_path / ".env"


def test_env_path_frozen_prefers_appdata(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("YOUTUBE_PIPELINE_ENV", raising=False)
    appdata = tmp_path / "appdata"
    install = tmp_path / "install"
    appdata.mkdir()
    install.mkdir()
    (appdata / ".env").write_text("GEMINI_API_KEY=from-appdata\n", encoding="utf-8")
    (install / ".env").write_text("GEMINI_API_KEY=from-install\n", encoding="utf-8")
    monkeypatch.setattr("desktop.paths.is_frozen", lambda: True)
    monkeypatch.setattr("desktop.paths.user_data_dir", lambda: appdata)
    monkeypatch.setattr("desktop.paths.install_root", lambda: install)
    dest = env_path()
    assert dest == appdata / ".env"
    assert dest.read_text(encoding="utf-8") == "GEMINI_API_KEY=from-appdata\n"


def test_env_path_migrates_from_install_root(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("YOUTUBE_PIPELINE_ENV", raising=False)
    appdata = tmp_path / "appdata"
    install = tmp_path / "install"
    appdata.mkdir()
    install.mkdir()
    (install / ".env").write_text("GEMINI_API_KEY=legacy\n", encoding="utf-8")
    monkeypatch.setattr("desktop.paths.is_frozen", lambda: True)
    monkeypatch.setattr("desktop.paths.user_data_dir", lambda: appdata)
    monkeypatch.setattr("desktop.paths.install_root", lambda: install)
    dest = env_path()
    assert dest == appdata / ".env"
    assert dest.is_file()
    assert dest.read_text(encoding="utf-8") == "GEMINI_API_KEY=legacy\n"
    assert (install / ".env").read_text(encoding="utf-8") == "GEMINI_API_KEY=legacy\n"


def test_migrate_install_env_does_not_overwrite_appdata(tmp_path: Path, monkeypatch) -> None:
    appdata = tmp_path / "appdata"
    install = tmp_path / "install"
    appdata.mkdir()
    install.mkdir()
    dest = appdata / ".env"
    dest.write_text("GEMINI_API_KEY=keep-me\n", encoding="utf-8")
    (install / ".env").write_text("GEMINI_API_KEY=ignore-me\n", encoding="utf-8")
    monkeypatch.setattr("desktop.paths.install_root", lambda: install)
    assert migrate_install_env(dest) == dest
    assert dest.read_text(encoding="utf-8") == "GEMINI_API_KEY=keep-me\n"
