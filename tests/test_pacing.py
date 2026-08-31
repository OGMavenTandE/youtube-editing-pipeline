from pipeline.config import Settings
from pipeline.layouts import LayoutKind
from pipeline.models import EditScript, GraphicCard, Scene
from pipeline.pacing import enforce_pacing, evaluate_pacing, expected_scene_range, graphic_is_real


def test_twenty_minute_band_is_about_50_to_80() -> None:
    low, high = expected_scene_range(20 * 60, Settings())
    assert low == 50
    assert 75 <= high <= 90


def test_empty_script_fills_20_minute_timeline() -> None:
    settings = Settings()
    duration = 20 * 60
    script = enforce_pacing(EditScript.empty(), duration, settings)
    report = evaluate_pacing(script, duration, settings)
    assert report.in_band
    assert report.scene_count >= 50
    assert script.scenes[0].start == 0.0
    assert abs(script.scenes[-1].end - duration) < 0.05
    assert script.scenes[0].layout is LayoutKind.FULL_FRAME
    assert report.micro_event_count > report.scene_count
    kinds = {event.kind for scene in script.scenes for event in scene.micro_events}
    assert "punch_in" in kinds
    assert "text" in kinds
    assert "cut" in kinds


def test_lazy_three_scene_script_is_split() -> None:
    settings = Settings()
    duration = 20 * 60
    lazy = EditScript(
        scenes=[
            Scene(start=0, end=400, layout=LayoutKind.FULL_FRAME, graphic=GraphicCard(title="A")),
            Scene(start=400, end=800, layout=LayoutKind.FULL_FRAME, graphic=GraphicCard(title="B")),
            Scene(start=800, end=1200, layout=LayoutKind.FULL_FRAME, graphic=GraphicCard(title="C")),
        ]
    )
    script = enforce_pacing(lazy, duration, settings)
    report = evaluate_pacing(script, duration, settings)
    assert len(script.scenes) >= 50
    assert report.in_band
    assert all(scene.layout is LayoutKind.FULL_FRAME for scene in script.scenes)
    assert all(scene.asset_kind == "none" for scene in script.scenes)


def test_pacing_fills_do_not_invent_empty_pip_cards() -> None:
    settings = Settings()
    duration = 90.0
    script = enforce_pacing(
        EditScript(
            scenes=[
                Scene(
                    start=20,
                    end=40,
                    layout=LayoutKind.PIP_BOTTOM_RIGHT,
                    asset_kind="card",
                    graphic=GraphicCard(
                        kicker="Real",
                        title="Real card",
                        bullets=["One", "Two"],
                        slide_id="real",
                    ),
                )
            ]
        ),
        duration,
        settings,
    )
    fills = [scene for scene in script.scenes if scene.reason == "pacing-fill"]
    assert fills
    for scene in fills:
        if scene.layout is not LayoutKind.FULL_FRAME:
            assert graphic_is_real(scene.graphic)
            assert scene.graphic.title
    for scene in script.scenes:
        if scene.layout is not LayoutKind.FULL_FRAME:
            assert graphic_is_real(scene.graphic)
            assert scene.graphic.title or scene.graphic.asset_path


def test_empty_script_fills_stay_full_frame() -> None:
    script = enforce_pacing(EditScript.empty(), 60.0, Settings())
    assert script.scenes
    assert all(scene.layout is LayoutKind.FULL_FRAME for scene in script.scenes)
    assert all(not graphic_is_real(scene.graphic) for scene in script.scenes)


def test_short_clip_stays_one_or_two_scenes() -> None:
    settings = Settings()
    script = enforce_pacing(EditScript.empty(), 3.0, settings)
    assert 1 <= len(script.scenes) <= 2
    assert abs(script.scenes[-1].end - 3.0) < 0.05
