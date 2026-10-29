from __future__ import annotations

from pathlib import Path

import pytest

from tests.ytp.mediautil import require_ffmpeg, write_clip, write_png, write_wav
from ytp.captions import format_srt_time, write_srt
from ytp.ff import concat_line, escape_filter_text, has_audio, probe_duration, probe_video_size
from ytp.hooks import STUDIO_GRAPHICS_STUB, TRACK_MIX_STUB
from ytp.plan import Broll, CaptionCue, EditPlan, MusicCue, NarrationLine, Segment, Short
from ytp.render import (
    allocate_render_path,
    music_filtergraph,
    prepare_drawtext_font,
    render_plan,
    resolve_media,
)


def _episode(tmp_path: Path) -> tuple[Path, EditPlan]:
    episode = tmp_path / "ep"
    write_png(episode / "frames" / "a.png", color="black")
    write_png(episode / "frames" / "b.png", color="white")
    write_wav(episode / "audio" / "voice" / "voice.wav", seconds=0.8, frequency=440)
    write_wav(episode / "audio" / "score" / "bed.wav", seconds=0.8, frequency=110)
    write_clip(episode / "source" / "raw" / "broll.mp4", color="navy", seconds=0.4)
    plan = EditPlan(
        series="ai-survival",
        episode="t",
        width=320,
        height=180,
        fps=30,
        voice="audio/voice/voice.wav",
        captions_mode="srt",
        segments=[
            Segment(id="a", kind="still", source="frames/a.png", start=0.0, duration=0.4),
            Segment(
                id="b",
                kind="still",
                source="frames/b.png",
                start=0.4,
                duration=0.4,
                template="card",
                template_text="Tip",
                palette="DARK",
            ),
        ],
        broll=[
            Broll(
                id="overlay",
                source="source/raw/broll.mp4",
                at=0.2,
                duration=0.3,
                layout="pip-lower-right",
            )
        ],
        music=[
            MusicCue(
                id="bed",
                sfx="synth-stab",
                at=0.0,
                gain_db=-20,
                source="audio/score/bed.wav",
                duration=0.8,
                duck_under_voice=True,
            )
        ],
        captions=[CaptionCue(start=0.0, end=0.4, text="Stay low.")],
        narration=[NarrationLine(id="n1", text="Stay low.", start=0.0, end=0.4)],
        shorts=[Short(id="A", title="Stay low", start=0.0, end=0.4)],
    )
    return episode, plan


def test_resolve_media_uses_the_episode_dir_not_the_cwd(tmp_path: Path, monkeypatch) -> None:
    episode = tmp_path / "ep"
    (episode / "frames").mkdir(parents=True)
    (episode / "frames" / "a.png").write_bytes(b"episode")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "frames").mkdir()
    (tmp_path / "frames" / "a.png").write_bytes(b"cwd")
    found = resolve_media("frames/a.png", episode)
    assert found == episode / "frames" / "a.png"
    assert found.read_bytes() == b"episode"
    windows = resolve_media(r"C:\AI Eval\YouTube\take.mp4", episode)
    assert str(windows) == r"C:\AI Eval\YouTube\take.mp4"


def test_captions_both_burns_in_and_writes_srt(tmp_path: Path, monkeypatch) -> None:
    require_ffmpeg()
    episode, plan = _episode(tmp_path)
    burned: list[Path] = []
    import ytp.render as render_mod

    real_burn = render_mod._burn

    def spy(video, srt, dest, encoder, preset):
        burned.append(dest)
        return real_burn(video, srt, dest, encoder, preset)

    monkeypatch.setattr(render_mod, "_burn", spy)
    out = episode / "renders" / "v1" / "cut.mp4"
    result = render_plan(plan, out, episode_dir=episode, preset="ultrafast", captions="both")
    assert burned
    assert result.srt is not None
    assert "Stay low." in result.srt.read_text(encoding="utf-8")
    assert result.video.is_file()


def test_versioned_render_folders_do_not_overwrite(tmp_path: Path) -> None:
    episode = tmp_path / "ep"
    first = allocate_render_path(episode, "cut.mp4")
    first.parent.mkdir(parents=True)
    first.write_bytes(b"keep")
    second = allocate_render_path(episode, "cut.mp4")
    assert first.parent.name == "v1"
    assert second.parent.name == "v2"
    assert first.read_bytes() == b"keep"
    with pytest.raises(FileExistsError):
        render_plan(
            EditPlan(
                width=320,
                height=180,
                segments=[
                    Segment(id="a", kind="still", source="missing.png", start=0, duration=0.2)
                ],
            ),
            first,
            episode_dir=episode,
        )
    assert first.read_bytes() == b"keep"


def test_filtergraph_ducks_only_when_asked() -> None:
    ducked = music_filtergraph(cues=[(0.0, 0.8, -20.0)], duration=0.8, duck=True)
    dry = music_filtergraph(cues=[(0.0, 0.8, -20.0)], duration=0.8, duck=False)
    assert "sidechaincompress" in ducked
    assert "sidechaincompress" not in dry
    assert "volume=-20.000dB" in ducked


def test_concat_and_filter_escapes(tmp_path: Path) -> None:
    path = tmp_path / "bar's.wav"
    path.write_bytes(b"")
    line = concat_line(path)
    assert line.startswith("file '")
    assert "bar'\\''s.wav'" in line
    assert escape_filter_text("C:/fonts/bold.ttf") == r"C\:/fonts/bold.ttf"
    assert "," not in escape_filter_text("bold.ttf")


def test_drawtext_font_is_copied_without_commas(tmp_path: Path) -> None:
    source = tmp_path / "DejaVu, Sans.ttf"
    source.write_bytes(b"font")
    dest = prepare_drawtext_font(source, tmp_path / "clean")
    assert dest.name == "bold.ttf"
    assert "," not in dest.as_posix()
    assert dest.read_bytes() == b"font"


def test_srt_clock() -> None:
    assert format_srt_time(1.5) == "00:00:01,500"
    assert format_srt_time(72.01) == "00:01:12,010"


def test_render_concat_broll_duck_and_srt(tmp_path: Path) -> None:
    require_ffmpeg()
    episode, plan = _episode(tmp_path)
    out = episode / "renders" / "cut.mp4"
    result = render_plan(
        plan,
        out,
        episode_dir=episode,
        preset="ultrafast",
        captions="both",
    )
    assert result.video.is_file()
    assert result.srt is not None and "Stay low." in result.srt.read_text(encoding="utf-8")
    width, height = probe_video_size(result.video)
    assert (width, height) == (320, 180)
    assert has_audio(result.video)
    assert probe_duration(result.video) == pytest.approx(0.8, abs=0.25)


def test_render_calls_track_and_studio_hooks(tmp_path: Path) -> None:
    require_ffmpeg()
    episode, plan = _episode(tmp_path)
    plan_path = episode / "edit" / "plan.yaml"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text("version: 1\n", encoding="utf-8")
    out = episode / "renders" / "hooked.mp4"
    result = render_plan(
        plan,
        out,
        episode_dir=episode,
        plan_path=plan_path,
        preset="ultrafast",
        captions="srt",
        audio_hook=TRACK_MIX_STUB,
        graphics_hook=STUDIO_GRAPHICS_STUB,
    )
    assert result.video.is_file()
    audio_cmd = next(cmd for cmd in result.commands if "--cues" in cmd)
    assert "--voice" in audio_cmd and "--out" in audio_cmd and "--plan" in audio_cmd
    graphic_cmd = next(cmd for cmd in result.commands if "--template" in cmd)
    assert "card" in graphic_cmd
    assert "Tip" in graphic_cmd
    assert has_audio(result.video)


def test_render_short_is_vertical(tmp_path: Path) -> None:
    require_ffmpeg()
    episode, plan = _episode(tmp_path)
    out = episode / "renders" / "cut.mp4"
    result = render_plan(
        plan,
        out,
        episode_dir=episode,
        preset="ultrafast",
        captions="srt",
        short_id="A",
    )
    assert result.short is not None
    assert probe_video_size(result.short) == (1080, 1920)


def test_write_srt_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "c.srt"
    write_srt([CaptionCue(0.0, 1.25, "Hello")], path)
    text = path.read_text(encoding="utf-8")
    assert "00:00:00,000 --> 00:00:01,250" in text
    assert "Hello" in text
