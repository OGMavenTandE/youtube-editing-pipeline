from pathlib import Path

from pipeline.broll.local import apply_local_broll, match_local_broll, query_tokens
from pipeline.layouts import PictureTag
from pipeline.models import BRollCue, EditScript, GraphicCard, Scene
from pipeline.stills import apply_stills, match_local_still


def test_local_broll_matches_query(tmp_path: Path | None = None) -> None:
    folder = Path("/tmp/yt-pipe-broll-match")
    folder.mkdir(parents=True, exist_ok=True)
    hit = folder / "city-night-drive.mp4"
    miss = folder / "office-whiteboard.mp4"
    hit.write_bytes(b"x")
    miss.write_bytes(b"x")
    found = match_local_broll("city night", folder)
    assert found is not None
    assert found.name == "city-night-drive.mp4"
    assert match_local_broll("underwater coral", folder) is None
    assert query_tokens("city night") == {"city", "night"}


def test_apply_local_still_stamps_pip_query() -> None:
    folder = Path("/tmp/yt-pipe-still-apply")
    folder.mkdir(parents=True, exist_ok=True)
    still = folder / "mq9-reaper.jpg"
    still.write_bytes(b"x")
    script = EditScript(
        scenes=[
            Scene(
                start=0,
                end=10,
                layout=PictureTag.PIP,
                shown="MQ-9 Reaper",
                graphic=GraphicCard(
                    kicker="$1.5B",
                    title="in procurements",
                    still_query="MQ-9 Reaper",
                ),
            )
        ],
        broll=[BRollCue(start=0, end=10, query="reaper")],
    )
    apply_stills(script, folder)
    assert script.scenes[0].graphic.asset_path.endswith("mq9-reaper.jpg")
    assert match_local_still("reaper", folder) is not None


def test_apply_local_broll_does_not_invent_for_nothing_scene() -> None:
    folder = Path("/tmp/yt-pipe-broll-none")
    folder.mkdir(parents=True, exist_ok=True)
    clip = folder / "bridge-repair.mp4"
    clip.write_bytes(b"x")
    script = EditScript(
        scenes=[
            Scene(
                start=0,
                end=10,
                layout=PictureTag.NOTHING,
                graphic=GraphicCard(title="Bridge repair"),
            )
        ]
    )
    apply_local_broll(script, folder)
    assert script.scenes[0].graphic.asset_path == ""
    assert script.scenes[0].layout is PictureTag.NOTHING
