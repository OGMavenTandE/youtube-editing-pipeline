from pipeline.models import EditScript, GraphicCard, Scene, SilenceCutMap, TimeRange
from pipeline.layouts import PictureTag
from pipeline.timeline import remap_edit_script


def test_remap_edit_script_applies_keep_ranges() -> None:
    cut_map = SilenceCutMap(
        kept_ranges=[TimeRange(start=0.0, end=2.0), TimeRange(start=4.0, end=6.0)],
        removed_ranges=[TimeRange(start=2.0, end=4.0)],
        original_duration=6.0,
        trimmed_duration=4.0,
    )
    script = EditScript(
        scenes=[
            Scene(start=0, end=2, layout=PictureTag.NOTHING, graphic=GraphicCard(title="A")),
            Scene(start=4, end=6, layout=PictureTag.NOTHING, graphic=GraphicCard(title="B")),
        ]
    )
    remapped = remap_edit_script(script, cut_map)
    assert remapped.scenes[0].start == 0.0
    assert remapped.scenes[0].end == 2.0
    assert remapped.scenes[1].start == 2.0
    assert remapped.scenes[1].end == 4.0
    assert remapped.talking_head_cuts == []
