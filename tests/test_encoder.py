from __future__ import annotations

import inspect
import logging
import subprocess
from pathlib import Path

from moviepy.video.VideoClip import VideoClip

from pipeline.compositor import _write_composed_clip
from pipeline.config import Settings
from pipeline.encoder import (
    MOVIEPY_WRITE_VIDEOFILE_KEYS,
    NVENC_CODEC,
    SOFTWARE_CODEC,
    encoder_is_listed,
    nvenc_encoder,
    remember_nvenc_failure,
    reset_encoder_cache,
    select_video_encoder,
    software_encoder,
)
from pipeline.media import MediaError, apply_loudnorm, concat_scene_files, _run_encode


def _settings() -> Settings:
    return Settings()


def _fake_hidden(monkeypatch, handler) -> None:
    monkeypatch.setattr("pipeline.encoder.require_ffmpeg", lambda settings=None: "ffmpeg")
    monkeypatch.setattr("pipeline.encoder.run_hidden", handler)


def _completed(args: list[str], *, code: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(args, code, stdout=stdout, stderr=stderr)


def test_selects_nvenc_when_listed_and_smoke_ok(monkeypatch, caplog) -> None:
    caplog.set_level(logging.INFO)

    def fake_run(args, **kwargs):
        if "-encoders" in args:
            assert "-hide_banner" in args
            return _completed(
                args,
                stdout=" V....D h264_nvenc           NVIDIA NVENC H.264 encoder\n"
                " V....D libx264              libx264 H.264\n",
            )
        assert NVENC_CODEC in args
        return _completed(args)

    _fake_hidden(monkeypatch, fake_run)
    choice = select_video_encoder(_settings())
    assert choice.name == NVENC_CODEC
    assert "encoder=h264_nvenc" in caplog.text
    assert select_video_encoder(_settings()) is choice


def test_falls_back_when_nvenc_not_listed(monkeypatch, caplog) -> None:
    caplog.set_level(logging.INFO)
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return _completed(args, stdout=" V....D libx264              libx264 H.264\n")

    _fake_hidden(monkeypatch, fake_run)
    choice = select_video_encoder(_settings())
    assert choice.name == SOFTWARE_CODEC
    assert "encoder=libx264" in caplog.text
    assert all("-encoders" in cmd for cmd in calls)
    assert all(NVENC_CODEC not in cmd for cmd in calls)


def test_falls_back_when_probe_fails(monkeypatch, caplog) -> None:
    caplog.set_level(logging.INFO)

    def fake_run(args, **kwargs):
        return _completed(args, code=1, stderr="ffmpeg exploded")

    _fake_hidden(monkeypatch, fake_run)
    assert select_video_encoder(_settings()).name == SOFTWARE_CODEC
    assert "encoder=libx264" in caplog.text


def test_falls_back_when_listed_but_smoke_encode_fails(monkeypatch, caplog) -> None:
    caplog.set_level(logging.INFO)

    def fake_run(args, **kwargs):
        if "-encoders" in args:
            return _completed(args, stdout=" V....D h264_nvenc           NVIDIA NVENC\n")
        return _completed(args, code=255, stderr="Cannot load nvcuda.dll")

    _fake_hidden(monkeypatch, fake_run)
    assert select_video_encoder(_settings()).name == SOFTWARE_CODEC
    assert "encoder=libx264" in caplog.text


def test_encoder_list_matches_token_not_substring() -> None:
    listed = " V....D hevc_nvenc           NVIDIA NVENC hevc\n V....D libx264              x264\n"
    assert not encoder_is_listed(listed, NVENC_CODEC)
    assert encoder_is_listed(
        " V....D h264_nvenc           NVIDIA NVENC H.264 encoder (codec h264)\n",
        NVENC_CODEC,
    )


def test_probe_uses_run_hidden_once(monkeypatch) -> None:
    calls = {"n": 0}

    def fake_run(args, **kwargs):
        calls["n"] += 1
        return _completed(args, stdout=" V....D libx264              libx264\n")

    _fake_hidden(monkeypatch, fake_run)
    select_video_encoder(_settings())
    select_video_encoder(_settings())
    assert calls["n"] == 1


def test_remember_nvenc_failure_pins_software(monkeypatch, caplog) -> None:
    caplog.set_level(logging.INFO)

    def fake_run(args, **kwargs):
        if "-encoders" in args:
            return _completed(args, stdout=" V....D h264_nvenc           NVIDIA\n")
        return _completed(args)

    _fake_hidden(monkeypatch, fake_run)
    assert select_video_encoder(_settings()).name == NVENC_CODEC
    remember_nvenc_failure("device lost")
    assert select_video_encoder(_settings()).name == SOFTWARE_CODEC
    assert "encoder=libx264" in caplog.text


def test_software_write_kwargs_match_current_path() -> None:
    kwargs = software_encoder().moviepy_write_kwargs(fps=30)
    assert kwargs == {
        "codec": "libx264",
        "audio_codec": "aac",
        "fps": 30,
        "preset": "medium",
        "threads": 0,
        "logger": None,
    }
    assert set(kwargs) <= MOVIEPY_WRITE_VIDEOFILE_KEYS


def test_nvenc_write_kwargs_are_valid_moviepy2() -> None:
    kwargs = nvenc_encoder().moviepy_write_kwargs(fps=29.97)
    assert kwargs["codec"] == NVENC_CODEC
    assert kwargs["audio_codec"] == "aac"
    assert kwargs["preset"] == "p4"
    assert kwargs["threads"] == 0
    assert kwargs["logger"] is None
    assert kwargs["ffmpeg_params"][kwargs["ffmpeg_params"].index("-pix_fmt") + 1] == "yuv420p"
    assert set(kwargs) <= MOVIEPY_WRITE_VIDEOFILE_KEYS


def test_write_kwargs_accepted_by_moviepy_signature() -> None:
    params = set(inspect.signature(VideoClip.write_videofile).parameters)
    params.discard("self")
    params.discard("filename")
    for kwargs in (
        software_encoder().moviepy_write_kwargs(fps=30),
        nvenc_encoder().moviepy_write_kwargs(fps=30),
    ):
        unknown = set(kwargs) - params
        assert not unknown, unknown


def test_ffmpeg_video_args_keep_aac_to_caller() -> None:
    software = software_encoder().ffmpeg_video_args(quality="medium")
    assert software[:4] == ["-c:v", "libx264", "-preset", "medium"]
    assert "-crf" in software
    nvenc = nvenc_encoder().ffmpeg_video_args(quality="medium")
    assert nvenc[:4] == ["-c:v", "h264_nvenc", "-preset", "p4"]
    assert "-c:a" not in nvenc
    fast = nvenc_encoder().ffmpeg_video_args(quality="veryfast")
    assert fast[3] == "p1"


def test_write_composed_clip_falls_back_when_nvenc_raises(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "pipeline.compositor.select_video_encoder", lambda settings: nvenc_encoder()
    )
    calls: list[dict[str, object]] = []

    class FakeClip:
        def write_videofile(self, path: str, **kwargs: object) -> None:
            calls.append(kwargs)
            if kwargs["codec"] == NVENC_CODEC:
                raise OSError("nvenc device busy")

    _write_composed_clip(FakeClip(), tmp_path / "out.mp4", 30, _settings())  # type: ignore[arg-type]
    assert calls[0]["codec"] == NVENC_CODEC
    assert calls[1]["codec"] == SOFTWARE_CODEC
    assert select_video_encoder(_settings()).name == SOFTWARE_CODEC


def test_concat_scene_files_uses_selected_encoder(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("pipeline.media.require_ffmpeg", lambda settings: "ffmpeg")
    monkeypatch.setattr(
        "pipeline.media.select_video_encoder", lambda settings: nvenc_encoder()
    )
    seen: list[list[str]] = []

    def fake_run(cmd: list[str], label: str) -> None:
        seen.append(cmd)
        Path(cmd[-1]).write_bytes(b"ok")

    monkeypatch.setattr("pipeline.media._run", fake_run)
    part = tmp_path / "scene.mp4"
    part.write_bytes(b"p")
    dest = tmp_path / "out.mp4"
    concat_scene_files([part], dest, Settings(work_dir=tmp_path, output_dir=tmp_path), loudnorm=True)
    assert NVENC_CODEC in seen[0]
    assert "aac" in seen[0]
    assert "libx264" not in seen[0]


def test_run_encode_retries_software_after_nvenc_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        "pipeline.media.select_video_encoder", lambda settings: nvenc_encoder()
    )
    seen: list[list[str]] = []

    def fake_run(cmd: list[str], label: str) -> None:
        seen.append(cmd)
        if NVENC_CODEC in cmd:
            raise MediaError("nvenc died")

    monkeypatch.setattr("pipeline.media._run", fake_run)
    _run_encode(
        _settings(),
        "encode",
        lambda encoder: ["ffmpeg", *encoder.ffmpeg_video_args(), "-c:a", "aac", "out.mp4"],
    )
    assert seen[0][2] == NVENC_CODEC
    assert seen[1][2] == SOFTWARE_CODEC


def test_apply_loudnorm_uses_software_args_when_selected(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("pipeline.media.require_ffmpeg", lambda settings: "ffmpeg")
    monkeypatch.setattr(
        "pipeline.media.select_video_encoder", lambda settings: software_encoder()
    )
    seen: list[list[str]] = []

    def fake_run(cmd: list[str], label: str) -> None:
        seen.append(cmd)

    monkeypatch.setattr("pipeline.media._run", fake_run)
    src = tmp_path / "in.mp4"
    src.write_bytes(b"x")
    apply_loudnorm(src, tmp_path / "out.mp4", _settings())
    assert seen[0][seen[0].index("-c:v") + 1] == SOFTWARE_CODEC
    assert "medium" in seen[0]
    assert "aac" in seen[0]


def test_reset_encoder_cache_clears_choice(monkeypatch) -> None:
    def fake_run(args, **kwargs):
        if "-encoders" in args:
            return _completed(args, stdout=" V....D h264_nvenc           NVIDIA\n")
        return _completed(args)

    _fake_hidden(monkeypatch, fake_run)
    assert select_video_encoder(_settings()).name == NVENC_CODEC
    reset_encoder_cache()

    def software_only(args, **kwargs):
        return _completed(args, stdout=" V....D libx264              x264\n")

    _fake_hidden(monkeypatch, software_only)
    assert select_video_encoder(_settings()).name == SOFTWARE_CODEC
