from pathlib import Path

from pipeline.broll.local import apply_local_broll, match_local_broll, query_tokens
from pipeline.layouts import LayoutKind
from pipeline.models import BRollCue, EditScript, GraphicCard, Scene


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


def test_apply_local_broll_stamps_scene_and_cue() -> None:
    folder = Path("/tmp/yt-pipe-broll-apply")
    folder.mkdir(parents=True, exist_ok=True)
    clip = folder / "bridge-repair.mp4"
    clip.write_bytes(b"x")
    script = EditScript(
        scenes=[
            Scene(
                start=0,
                end=10,
                layout=LayoutKind.PIP_BOTTOM_RIGHT,
                asset_kind="broll",
                shown="bridge repair",
                graphic=GraphicCard(title="Bridge repair"),
            )
        ],
        broll=[BRollCue(start=0, end=10, query="bridge")],
    )
    apply_local_broll(script, folder)
    assert script.scenes[0].graphic.asset_path.endswith("bridge-repair.mp4")
    assert script.scenes[0].asset_ref is not None
    assert script.scenes[0].asset_ref.endswith("bridge-repair.mp4")
    assert script.broll[0].asset_path is not None
    assert script.broll[0].asset_path.endswith("bridge-repair.mp4")


def test_apply_local_broll_does_not_invent_for_none_scene() -> None:
    folder = Path("/tmp/yt-pipe-broll-none")
    folder.mkdir(parents=True, exist_ok=True)
    clip = folder / "bridge-repair.mp4"
    clip.write_bytes(b"x")
    script = EditScript(
        scenes=[
            Scene(
                start=0,
                end=10,
                layout=LayoutKind.PIP_BOTTOM_RIGHT,
                asset_kind="none",
                graphic=GraphicCard(title="Bridge repair"),
            )
        ]
    )
    apply_local_broll(script, folder)
    assert script.scenes[0].graphic.asset_path == ""
    assert script.scenes[0].asset_kind == "none"
