from __future__ import annotations

import subprocess

from pipeline.hidden_process import (
    CREATE_NO_WINDOW,
    hidden_popen_kwargs,
    install_hidden_subprocess,
    run_hidden,
)


def test_hidden_popen_kwargs_noop_off_windows(monkeypatch) -> None:
    monkeypatch.setattr("pipeline.hidden_process.sys.platform", "linux")
    assert hidden_popen_kwargs() == {}


def test_hidden_popen_kwargs_win32_sets_creationflags(monkeypatch) -> None:
    monkeypatch.setattr("pipeline.hidden_process.sys.platform", "win32")
    kwargs = hidden_popen_kwargs()
    assert kwargs["creationflags"] == CREATE_NO_WINDOW
    assert kwargs["creationflags"] == 0x08000000


def test_run_hidden_passes_creationflags_on_win32(monkeypatch) -> None:
    monkeypatch.setattr("pipeline.hidden_process.sys.platform", "win32")
    seen: dict[str, object] = {}

    def fake_run(args, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr("pipeline.hidden_process.subprocess.run", fake_run)
    run_hidden(["ffmpeg", "-version"], capture_output=True, text=True)
    assert seen["creationflags"] == CREATE_NO_WINDOW
    assert seen["capture_output"] is True


def test_run_hidden_is_noop_off_windows(monkeypatch) -> None:
    monkeypatch.setattr("pipeline.hidden_process.sys.platform", "linux")
    seen: dict[str, object] = {}

    def fake_run(args, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr("pipeline.hidden_process.subprocess.run", fake_run)
    run_hidden(["ffprobe", "-version"], capture_output=True)
    assert "creationflags" not in seen
    assert "startupinfo" not in seen


def test_install_hidden_subprocess_noop_off_windows(monkeypatch) -> None:
    monkeypatch.setattr("pipeline.hidden_process.sys.platform", "linux")
    original = subprocess.Popen
    install_hidden_subprocess()
    assert subprocess.Popen is original
