"""Windows path handling, encoder choice, and the take/finish flow. No GPU."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.ytp.mediautil import require_ffmpeg, write_clip, write_png, write_wav
from ytp.ff import ffmpeg_bin, ffprobe_bin
from ytp.finish import finish_episode
from ytp.plan import EditPlan, NarrationLine, Segment, save_plan
from ytp.render import choose_encoder, parse_encoder_list, resolve_encoder
from ytp.take import clock, episode_dir_for_take, ingest_take, link_take, transcript_text
from ytp.transcribe import Transcript, Word, enable_nvidia_dlls, nvidia_dll_dirs


class _Script:
    name = "small"

    def transcribe(self, media: Path, *, start: float | None = None, end: float | None = None):
        del media, start, end
        return Transcript(
            text="These are my own views.",
            language="en",
            word_timestamps=True,
            segments=[(0.2, 1.4, "These are my own views.")],
            words=[Word("These", 0.2, 0.5, 0.9), Word("views.", 1.0, 1.4, 0.9)],
        )


def test_ytp_ffmpeg_and_windows_paths_with_spaces(tmp_path: Path, monkeypatch) -> None:
    bindir = tmp_path / "AI Eval" / "tools"
    bindir.mkdir(parents=True)
    ffmpeg = bindir / "ffmpeg.exe"
    ffprobe = bindir / "ffprobe.exe"
    ffmpeg.write_text("ffmpeg", encoding="utf-8")
    ffprobe.write_text("ffprobe", encoding="utf-8")
    monkeypatch.setenv("YTP_FFMPEG", str(ffmpeg))
    monkeypatch.delenv("FFMPEG_PATH", raising=False)
    monkeypatch.delenv("YTP_FFPROBE", raising=False)
    monkeypatch.delenv("FFPROBE_PATH", raising=False)
    assert ffmpeg_bin() == str(ffmpeg)
    assert ffprobe_bin() == str(ffprobe)
    assert " " in str(ffmpeg)

    folder = tmp_path / "AI Eval" / "YouTube"
    video = folder / "desk take.mp4"
    write_clip(video, seconds=0.4, size="320x180")
    episode = episode_dir_for_take(video, None)
    assert episode == folder / "desk take"
    original = video.read_bytes()
    linked = link_take(video, episode)
    assert video.read_bytes() == original
    assert linked.exists()
    assert os.path.samefile(video, linked)


def test_take_reference_when_hardlink_fails(tmp_path: Path, monkeypatch) -> None:
    video = tmp_path / "AI Eval" / "clip.mov"
    write_clip(video, seconds=0.3, size="160x120")
    original = video.stat().st_mtime_ns

    def _boom(src, dst):
        raise OSError("cross device")

    monkeypatch.setattr(os, "link", _boom)
    episode = tmp_path / "AI Eval" / "clip"
    episode.mkdir(parents=True)
    found = link_take(video, episode)
    assert found == video.resolve()
    assert video.stat().st_mtime_ns == original
    pointer = (episode / "edit" / "take.path").read_text(encoding="utf-8").strip()
    assert pointer == str(video.resolve())
    assert not (episode / "source" / "raw" / video.name).exists()


def test_take_writes_transcript_words_and_index(tmp_path: Path) -> None:
    video = tmp_path / "desk.mp4"
    write_clip(video, seconds=0.4, size="160x120")
    before = video.read_bytes()
    episode = ingest_take(video, series="talking-head", episode=None, transcriber=_Script())
    assert video.read_bytes() == before
    text = (episode / "edit" / "transcript.txt").read_text(encoding="utf-8")
    assert text.startswith("[0:00.20-0:01.40] These are my own views.")
    words = (episode / "edit" / "words.json").read_text(encoding="utf-8")
    assert '"model": "small"' in words
    assert (episode / "edit" / "clip-index.json").is_file()
    assert (episode / "source" / "raw" / "desk.mp4").is_file()
    assert clock(65.2) == "1:05.20"


def test_encoder_auto_falls_back_without_a_gpu() -> None:
    sample = (
        " V....D h264_nvenc           NVIDIA NVENC H.264 encoder\n"
        " V....D libx264              libx264\n"
    )
    listed = parse_encoder_list(sample)
    assert "h264_nvenc" in listed
    assert choose_encoder("auto", listed=listed, gpu_ok=False) == "libx264"
    assert choose_encoder("auto", listed=listed, gpu_ok=True) == "h264_nvenc"
    assert choose_encoder("auto", listed={"libx264"}, gpu_ok=True) == "libx264"
    assert choose_encoder("libx264", listed=listed, gpu_ok=True) == "libx264"
def test_encoder_auto_follows_the_nvenc_trial(monkeypatch) -> None:
    monkeypatch.setattr("ytp.render.ffmpeg_encoders", lambda: {"h264_nvenc", "libx264"})
    monkeypatch.setattr("ytp.render.nvenc_works", lambda: False)
    assert resolve_encoder("auto") == "libx264"
    monkeypatch.setattr("ytp.render.nvenc_works", lambda: True)
    assert resolve_encoder("auto") == "h264_nvenc"


def test_finish_renders_and_prints_the_report(tmp_path: Path, monkeypatch) -> None:
    require_ffmpeg()
    episode = tmp_path / "AI Eval" / "ep"
    write_png(episode / "frames" / "a.png")
    write_wav(episode / "audio" / "voice" / "voice.wav", seconds=0.6)
    plan = EditPlan(
        series="ai-survival",
        episode="ep",
        width=320,
        height=180,
        voice="audio/voice/voice.wav",
        segments=[Segment(id="a", kind="still", source="frames/a.png", start=0.0, duration=0.5)],
        narration=[NarrationLine(id="n", text="Stay low.", start=0.0, end=0.4)],
    )
    save_plan(plan, episode / "edit" / "plan.yaml")
    (episode / "edit" / "words.json").write_text(
        '{"words":[{"word":"Stay","start":0.0,"end":0.2},{"word":"low.","start":0.2,"end":0.4}]}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("ytp.finish.measure_loudness", lambda _path: (-16.0, -2.0))
    code = finish_episode(episode, rules_root=Path(__file__).resolve().parents[2])
    report = episode / "qc" / "check-report.md"
    assert report.is_file()
    printed = report.read_text(encoding="utf-8")
    assert "Render check" in printed
    assert list((episode / "renders").glob("v*/plan.mp4"))
    assert code == 0


def test_nvidia_dll_hook_is_a_noop_off_windows() -> None:
    if os.name == "nt":
        pytest.skip("Windows adds DLL directories")
    assert enable_nvidia_dlls() == []


def test_nvidia_dll_dirs_come_from_site_packages(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "site-packages"
    for name, dll in (
        ("cublas", "cublas64_12.dll"),
        ("cudnn", "cudnn64_9.dll"),
        ("cuda_nvrtc", "nvrtc64_120_0.dll"),
    ):
        folder = root / "nvidia" / name / "bin"
        folder.mkdir(parents=True)
        (folder / dll).write_bytes(b"dll")
    found = nvidia_dll_dirs([root, tmp_path / "empty"])
    assert [path.parent.name for path in found] == ["cublas", "cudnn", "cuda_nvrtc"]
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.delenv("PATH", raising=False)
    added = enable_nvidia_dlls([root])
    assert added == [str(path) for path in found]
    assert os.environ["PATH"].split(os.pathsep)[:3] == added


def test_missing_ffmpeg_override_is_reported(tmp_path: Path, monkeypatch) -> None:
    from ytp.selftest import _tool_status

    missing = tmp_path / "AI Eval" / "ffmpeg.exe"
    monkeypatch.setenv("YTP_FFMPEG", str(missing))
    monkeypatch.delenv("FFMPEG_PATH", raising=False)
    line = _tool_status("ffmpeg", ffmpeg_bin)
    assert line == f"ffmpeg: missing ({missing})"


def test_self_test_failure_exits_nonzero() -> None:
    from ytp.selftest import self_test_failed

    assert self_test_failed(["whisper: failed (no model)"])
    assert self_test_failed(["ffmpeg: missing (not on PATH)"])
    assert self_test_failed(["whisper: not installed"])
    assert not self_test_failed(["whisper: ok device=cpu words=1", "nvenc: no"])


def test_transcript_is_one_line_per_sentence() -> None:
    text = transcript_text(
        Transcript(
            text="First sentence. Second one.",
            words=[
                Word("First", 0.0, 0.3),
                Word("sentence.", 0.3, 0.8),
                Word("Second", 1.2, 1.5),
                Word("one.", 1.5, 1.9),
            ],
        )
    )
    assert text == "[0:00.00-0:00.80] First sentence.\n[0:01.20-0:01.90] Second one.\n"


def test_transcript_line_without_segments() -> None:
    text = transcript_text(
        Transcript(text="Hello there.", words=[Word("Hello", 1.0, 1.2), Word("there.", 1.2, 1.5)])
    )
    assert text == "[0:01.00-0:01.50] Hello there.\n"
