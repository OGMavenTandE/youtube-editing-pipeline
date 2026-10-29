from __future__ import annotations

import json
from pathlib import Path

from tests.ytp.mediautil import require_ffmpeg, write_wav
from ytp.cli import main
from ytp.index import index_episode
from ytp.layout import init_episode
from ytp.transcribe import Transcript, Word


class ScriptedTranscriber:
    name = "scripted"

    def transcribe(self, media: Path, *, start: float | None = None, end: float | None = None):
        return Transcript(
            text="Stay low.",
            language="en",
            words=[Word("Stay", 0.0, 0.2), Word("low.", 0.2, 0.4)],
        )


def test_index_writes_timestamps_and_keeps_description(tmp_path: Path) -> None:
    require_ffmpeg()
    episode = tmp_path / "ep01"
    init_episode(episode)
    write_wav(episode / "source" / "vo" / "line.wav", seconds=0.4)
    first = index_episode(episode, ScriptedTranscriber())
    clip = first["clips"][0]
    assert clip["path"] == "source/vo/line.wav"
    assert clip["duration"] == 0.4
    assert clip["transcript"] == "Stay low."
    assert clip["description"] == ""
    assert clip["timestamps"][0] == {"word": "Stay", "start": 0.0, "end": 0.2}
    index_path = episode / "edit" / "clip-index.json"
    document = json.loads(index_path.read_text(encoding="utf-8"))
    document["clips"][0]["description"] = "Cold open line"
    index_path.write_text(json.dumps(document), encoding="utf-8")
    second = index_episode(episode, ScriptedTranscriber())
    assert second["clips"][0]["description"] == "Cold open line"
    markdown = (episode / "edit" / "clip-index.md").read_text(encoding="utf-8")
    assert "source/vo/line.wav" in markdown
    assert "Cold open line" in markdown


def test_index_cli_backend_none(tmp_path: Path) -> None:
    require_ffmpeg()
    episode = tmp_path / "ep01"
    init_episode(episode)
    write_wav(episode / "source" / "vo" / "line.wav", seconds=0.3)
    assert main(["index", str(episode), "--backend", "none"]) == 0
    document = json.loads((episode / "edit" / "clip-index.json").read_text(encoding="utf-8"))
    assert document["backend"] == "none"
    assert document["clips"][0]["transcript"] == ""
    assert document["series"] == "ai-survival"
