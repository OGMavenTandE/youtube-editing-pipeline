from __future__ import annotations

import inspect
import subprocess

from pipeline.hidden_process import (
    CREATE_NO_WINDOW,
    HiddenPopen,
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


def test_hidden_popen_is_a_real_popen_subclass() -> None:
    assert inspect.isclass(HiddenPopen)
    assert not inspect.isfunction(HiddenPopen)
    assert issubclass(HiddenPopen, subprocess.Popen)


def test_child_can_subclass_popen_after_hidden_popen_assigned() -> None:
    """asyncio/windows_utils.py does `class Popen(subprocess.Popen)` on Windows."""
    original = subprocess.Popen
    try:
        subprocess.Popen = HiddenPopen
        class Child(subprocess.Popen):
            pass

        assert inspect.isclass(subprocess.Popen)
        assert not inspect.isfunction(subprocess.Popen)
        assert issubclass(Child, HiddenPopen)
        assert issubclass(Child, original)
    finally:
        subprocess.Popen = original


def test_install_assigns_hidden_popen_class_on_win32(monkeypatch) -> None:
    monkeypatch.setattr("pipeline.hidden_process.sys.platform", "win32")
    monkeypatch.setattr("pipeline.hidden_process._original_popen", None)
    original = subprocess.Popen
    try:
        install_hidden_subprocess()
        assert subprocess.Popen is HiddenPopen
        assert inspect.isclass(subprocess.Popen)
        assert not inspect.isfunction(subprocess.Popen)
        install_hidden_subprocess()
        assert subprocess.Popen is HiddenPopen

        class Child(subprocess.Popen):
            pass

        assert issubclass(Child, HiddenPopen)
    finally:
        subprocess.Popen = original


def test_hidden_popen_init_ors_creationflags_on_win32(monkeypatch) -> None:
    monkeypatch.setattr("pipeline.hidden_process.sys.platform", "win32")
    seen: dict[str, object] = {}

    def fake_init(self, *args, **kwargs):
        seen["args"] = args
        seen.update(kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", fake_init)
    HiddenPopen(["ffmpeg", "-version"], creationflags=0x1)
    assert seen["args"] == (["ffmpeg", "-version"],)
    assert seen["creationflags"] == (0x1 | CREATE_NO_WINDOW)
