from __future__ import annotations

from pathlib import Path

import yaml

from ytp.layout import EPISODE_FOLDERS, init_episode


def test_init_creates_handoff_folders(tmp_path: Path) -> None:
    episode = tmp_path / "ep01"
    manifest = init_episode(episode, "ai-survival")
    for folder in EPISODE_FOLDERS:
        assert (episode / folder).is_dir(), folder
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    assert data["series"] == "ai-survival"
    assert data["layout_version"] == 1
    assert data["folders"] == list(EPISODE_FOLDERS)


def test_init_is_idempotent_and_keeps_edit_files(tmp_path: Path) -> None:
    episode = tmp_path / "ep01"
    init_episode(episode)
    note = episode / "edit" / "plan.yaml"
    note.write_text("keep\n", encoding="utf-8")
    created = yaml.safe_load((episode / "episode.yaml").read_text(encoding="utf-8"))["created"]
    init_episode(episode, "other-series")
    assert note.read_text(encoding="utf-8") == "keep\n"
    again = yaml.safe_load((episode / "episode.yaml").read_text(encoding="utf-8"))
    assert again["series"] == "ai-survival"
    assert again["created"] == created
    init_episode(episode, "other-series", force=True)
    forced = yaml.safe_load((episode / "episode.yaml").read_text(encoding="utf-8"))
    assert forced["series"] == "other-series"


def test_init_cli(tmp_path: Path) -> None:
    from ytp.cli import main

    episode = tmp_path / "ep02"
    assert main(["init", str(episode), "--series", "ai-survival"]) == 0
    assert (episode / "qc").is_dir()
    assert (episode / "audio" / "mix").is_dir()
    assert (episode / "source" / "vo").is_dir()
