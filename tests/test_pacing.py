from pipeline.config import Settings
from pipeline.layouts import PictureTag
from pipeline.models import EditScript, GraphicCard, Scene
from pipeline.pacing import enforce_pacing, evaluate_pacing, expected_scene_range, graphic_is_real


def test_twenty_minute_band_is_sparse() -> None:
    low, high = expected_scene_range(20 * 60, Settings())
    assert low == 3
    assert high <= 40
    assert high >= 10


def test_empty_script_gets_bookends_not_fifty_scenes() -> None:
    settings = Settings(bookend_seconds=10)
    duration = 20 * 60
    script = enforce_pacing(EditScript.empty(), duration, settings)
    report = evaluate_pacing(script, duration, settings)
    assert report.in_band
    assert script.scenes[0].start == 0.0
    assert abs(script.scenes[-1].end - duration) < 0.05
    assert script.scenes[0].role == "open"
    assert script.scenes[-1].role == "close"
    assert script.scenes[0].layout is PictureTag.LOWER_THIRD
    assert report.micro_event_count == 0
    kinds = {event.kind for scene in script.scenes for event in scene.micro_events}
    assert "punch_in" not in kinds
    assert "text" not in kinds


def test_lazy_script_stays_nothing_in_the_body() -> None:
    settings = Settings(bookend_seconds=10)
    duration = 20 * 60
    lazy = EditScript(
        scenes=[
            Scene(start=0, end=400, layout=PictureTag.NOTHING, graphic=GraphicCard(title="A")),
            Scene(start=400, end=800, layout=PictureTag.NOTHING, graphic=GraphicCard(title="B")),
            Scene(start=800, end=1200, layout=PictureTag.NOTHING, graphic=GraphicCard(title="C")),
        ]
    )
    script = enforce_pacing(lazy, duration, settings)
    body = [scene for scene in script.scenes if scene.role == "body"]
    assert body
    assert all(scene.layout is PictureTag.NOTHING for scene in body)
    assert all(scene.asset_kind == "none" for scene in body)
    assert all(not scene.micro_events for scene in script.scenes)


def test_pacing_fills_do_not_invent_pip() -> None:
    settings = Settings(bookend_seconds=10)
    duration = 90.0
    script = enforce_pacing(
        EditScript(
            scenes=[
                Scene(
                    start=20,
                    end=40,
                    layout=PictureTag.OVERLAY,
                    graphic=GraphicCard(
                        kicker="THE MONEY",
                        title="$1.5B is the floor.",
                        icon="bar_chart",
                    ),
                )
            ]
        ),
        duration,
        settings,
    )
    fills = [scene for scene in script.scenes if scene.reason == "pacing-fill"]
    for scene in fills:
        assert scene.layout is PictureTag.NOTHING
    body_chrome = [
        scene
        for scene in script.scenes
        if scene.role == "body" and scene.layout is PictureTag.OVERLAY
    ]
    assert body_chrome
    assert graphic_is_real(body_chrome[0].graphic)


def test_empty_script_body_is_nothing() -> None:
    script = enforce_pacing(EditScript.empty(), 60.0, Settings(bookend_seconds=10))
    body = [scene for scene in script.scenes if scene.role == "body"]
    assert body
    assert all(scene.layout is PictureTag.NOTHING for scene in body)
    assert all(not graphic_is_real(scene.graphic) for scene in body)


def test_short_clip_is_bookends() -> None:
    settings = Settings(bookend_seconds=10)
    script = enforce_pacing(EditScript.empty(), 3.0, settings)
    assert 1 <= len(script.scenes) <= 2
    assert abs(script.scenes[-1].end - 3.0) < 0.05
    assert script.scenes[0].role == "open"
