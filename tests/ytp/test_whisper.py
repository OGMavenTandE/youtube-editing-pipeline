"""Local faster-whisper smoke test. CI skips this marker."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.ytp.mediautil import require_ffmpeg, write_wav
from ytp.index import index_episode
from ytp.layout import init_episode
from ytp.transcribe import FasterWhisperTranscriber

pytestmark = pytest.mark.whisper


@pytest.mark.whisper
def test_index_faster_whisper_on_a_short_tone(tmp_path: Path) -> None:
    pytest.importorskip("faster_whisper")
    require_ffmpeg()
    episode = tmp_path / "ep"
    init_episode(episode)
    write_wav(episode / "source" / "vo" / "tone.wav", seconds=1.0, frequency=220)
    document = index_episode(episode, FasterWhisperTranscriber(model="tiny", device="cpu"))
    clip = document["clips"][0]
    assert clip["path"] == "source/vo/tone.wav"
    assert clip["duration"] == 1.0
    assert "timestamps" in clip
