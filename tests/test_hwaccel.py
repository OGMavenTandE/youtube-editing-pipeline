from __future__ import annotations

import subprocess
from pathlib import Path

from pipeline.config import Settings
from pipeline.hwaccel import gpu_filters_available, reset_hwaccel_cache, select_hw_decode


def _completed(args: list[str], *, code: int = 0):
    return subprocess.CompletedProcess(args, code, stdout="", stderr="")


def test_hw_decode_skipped_when_disabled(tmp_path: Path) -> None:
    sample = tmp_path / "talk.mp4"
    sample.write_bytes(b"x")
    assert select_hw_decode(Settings(), sample, enabled=False) is None


def test_hw_decode_picks_first_working_probe(monkeypatch, tmp_path: Path) -> None:
    sample = tmp_path / "talk.mp4"
    sample.write_bytes(b"x")
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        if "h264_cuvid" in args:
            return _completed(args, code=1)
        if "d3d11va" in args:
            return _completed(args, code=0)
        return _completed(args, code=1)

    monkeypatch.setattr("pipeline.hwaccel.require_ffmpeg", lambda settings=None: "ffmpeg")
    monkeypatch.setattr("pipeline.hwaccel.run_hidden", fake_run)
    choice = select_hw_decode(Settings(), sample, enabled=True)
    assert choice is not None
    assert choice.name == "d3d11va"
    assert select_hw_decode(Settings(), sample, enabled=True) is choice
    assert sum("h264_cuvid" in cmd for cmd in calls) == 1


def test_gpu_filters_false_when_probe_fails(monkeypatch) -> None:
    def fake_run(args, **kwargs):
        return _completed(args, code=1)

    monkeypatch.setattr("pipeline.hwaccel.require_ffmpeg", lambda settings=None: "ffmpeg")
    monkeypatch.setattr("pipeline.hwaccel.run_hidden", fake_run)
    assert gpu_filters_available(Settings()) is False
    reset_hwaccel_cache()
