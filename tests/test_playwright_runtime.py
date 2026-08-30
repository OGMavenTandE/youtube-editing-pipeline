from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from desktop.ffmpeg_check import _playwright_install_argv, install_playwright_chromium
from desktop.playwright_runtime import (
    bundled_playwright_browsers_dir,
    configure_playwright_browsers,
    copy_chromium_into_dist,
    find_headless_shell,
    playwright_install_argv,
    playwright_install_environ,
    playwright_user_browsers_dir,
    require_dist_headless_shell,
    seed_user_browsers_from_bundled,
)
from desktop.worker import prepare_runtime_env


@pytest.fixture(autouse=True)
def _isolate_playwright_browsers_path(monkeypatch: pytest.MonkeyPatch) -> None:
    # setenv first so pytest records the original and can undo later writes.
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "")
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)


def _write_file(path: Path, data: bytes = b"fake") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _driver_tree(root: Path) -> tuple[Path, Path]:
    node = _write_file(root / "_internal" / "playwright" / "driver" / "node.exe")
    cli = root / "_internal" / "playwright" / "driver" / "package" / "cli.js"
    cli.parent.mkdir(parents=True, exist_ok=True)
    cli.write_text("cli", encoding="utf-8")
    return node, cli


def _headless_tree(browsers: Path, revision: str = "1234") -> Path:
    exe = (
        browsers
        / f"chromium_headless_shell-{revision}"
        / "chrome-headless-shell-win64"
        / "chrome-headless-shell.exe"
    )
    _write_file(exe)
    chrome = browsers / f"chromium-{revision}" / "chrome-win64" / "chrome.exe"
    _write_file(chrome)
    return exe


def test_playwright_browsers_path_prefers_user_data_dir(monkeypatch, tmp_path: Path) -> None:
    appdata = tmp_path / "Roaming" / "YouTubePipeline"
    monkeypatch.setattr("desktop.paths.user_data_dir", lambda: appdata)
    assert playwright_user_browsers_dir() == appdata / "ms-playwright"


def test_frozen_install_argv_uses_bundled_node_and_cli(monkeypatch, tmp_path: Path) -> None:
    install = tmp_path / "youtube-pipeline"
    node, cli = _driver_tree(install)
    monkeypatch.setattr("desktop.paths.is_frozen", lambda: True)
    monkeypatch.setattr("desktop.paths.install_root", lambda: install)
    argv = playwright_install_argv()
    assert argv == [str(node), str(cli), "install", "chromium"]
    assert _playwright_install_argv() == argv
    assert "-m" not in argv
    assert "python" not in Path(argv[0]).name.lower()


def test_source_install_argv_uses_this_python(monkeypatch) -> None:
    monkeypatch.setattr("desktop.paths.is_frozen", lambda: False)
    argv = playwright_install_argv()
    assert argv is not None
    assert argv[1:4] == ["-m", "playwright", "install"]
    assert argv[4] == "chromium"


def test_frozen_install_argv_none_without_driver(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("desktop.paths.is_frozen", lambda: True)
    monkeypatch.setattr("desktop.paths.install_root", lambda: tmp_path / "empty")
    assert playwright_install_argv() is None


def test_frozen_install_environ_points_at_appdata(monkeypatch, tmp_path: Path) -> None:
    dest = tmp_path / "YouTubePipeline" / "ms-playwright"
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.setattr("desktop.paths.is_frozen", lambda: True)
    env = playwright_install_environ({"PATH": "/bin"}, dest=dest)
    assert env["PLAYWRIGHT_BROWSERS_PATH"] == str(dest)
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(dest)
    assert dest.is_dir()


def test_configure_prefers_user_dir_and_seeds_from_bundle(monkeypatch, tmp_path: Path) -> None:
    install = tmp_path / "install"
    user = tmp_path / "appdata" / "ms-playwright"
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    bundled = bundled_playwright_browsers_dir(install)
    exe = _headless_tree(bundled)
    chosen = configure_playwright_browsers(frozen=True, root=install, dest=user)
    assert chosen == user
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(user)
    assert (user / exe.parent.parent.name / exe.parent.name / exe.name).is_file()


def test_seed_does_not_overwrite_existing_appdata(tmp_path: Path) -> None:
    bundled = tmp_path / "bundled"
    user = tmp_path / "user"
    _headless_tree(bundled, "1234")
    existing = _write_file(
        user
        / "chromium_headless_shell-1234"
        / "chrome-headless-shell-win64"
        / "chrome-headless-shell.exe",
        b"keep",
    )
    seed_user_browsers_from_bundled(bundled, user)
    assert existing.read_bytes() == b"keep"


def test_configure_falls_back_to_bundle_when_copy_fails(monkeypatch, tmp_path: Path) -> None:
    install = tmp_path / "install"
    user = tmp_path / "appdata" / "ms-playwright"
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    bundled = bundled_playwright_browsers_dir(install)
    _headless_tree(bundled)

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("desktop.playwright_runtime.seed_user_browsers_from_bundled", boom)
    chosen = configure_playwright_browsers(frozen=True, root=install, dest=user)
    assert chosen == bundled
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(bundled)


def test_prepare_runtime_env_sets_browsers_path_when_frozen(monkeypatch, tmp_path: Path) -> None:
    install = tmp_path / "install"
    appdata = tmp_path / "appdata"
    install.mkdir()
    appdata.mkdir()
    monkeypatch.setattr("desktop.paths.is_frozen", lambda: True)
    monkeypatch.setattr("desktop.paths.install_root", lambda: install)
    monkeypatch.setattr("desktop.paths.user_data_dir", lambda: appdata)
    monkeypatch.delenv("YOUTUBE_PIPELINE_ENV", raising=False)
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    prepare_runtime_env()
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(appdata / "ms-playwright")


def test_copy_and_require_dist_headless_shell(tmp_path: Path) -> None:
    src = tmp_path / "src-browsers"
    exe = _headless_tree(src)
    dist = tmp_path / "dist" / "youtube-pipeline"
    copy_chromium_into_dist(dist, src)
    found = require_dist_headless_shell(dist)
    assert found.name == "chrome-headless-shell.exe"
    assert found.is_file()
    assert find_headless_shell(src) == exe


def test_require_dist_headless_shell_missing(tmp_path: Path) -> None:
    try:
        require_dist_headless_shell(tmp_path / "empty-dist")
    except FileNotFoundError as exc:
        assert "chrome-headless-shell.exe" in str(exc)
    else:
        raise AssertionError("missing headless shell should fail the CI check")


def test_windows_workflow_installs_and_bundles_chromium() -> None:
    yml = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "windows-app.yml"
    text = yml.read_text(encoding="utf-8")
    assert "playwright install chromium" in text
    assert "PLAYWRIGHT_BROWSERS_PATH" in text
    assert "desktop.playwright_runtime" in text
    assert "playwright install firefox" not in text
    assert "playwright install webkit" not in text


def test_spec_collects_local_browsers() -> None:
    spec = Path(__file__).resolve().parent.parent / "desktop" / "youtube-pipeline.spec"
    text = spec.read_text(encoding="utf-8")
    assert ".local-browsers" in text
    assert "playwright/driver/package/.local-browsers" in text


def test_frozen_wizard_install_uses_bundled_node_hidden(monkeypatch, tmp_path: Path) -> None:
    install = tmp_path / "install"
    appdata = tmp_path / "appdata"
    node, cli = _driver_tree(install)
    dest = appdata / "ms-playwright"
    monkeypatch.setattr("desktop.paths.is_frozen", lambda: True)
    monkeypatch.setattr("desktop.paths.install_root", lambda: install)
    monkeypatch.setattr("desktop.paths.user_data_dir", lambda: appdata)
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    seen: dict[str, object] = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen.update(kwargs)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr("pipeline.hidden_process.subprocess.run", fake_run)
    monkeypatch.setattr("desktop.ffmpeg_check.playwright_chromium_ok", lambda: True)
    ok, message = install_playwright_chromium()
    assert ok
    assert message == "Chromium is installed."
    assert seen["args"] == [str(node), str(cli), "install", "chromium"]
    env = seen["env"]
    assert isinstance(env, dict)
    assert env["PLAYWRIGHT_BROWSERS_PATH"] == str(dest)
    assert "-m" not in seen["args"]
