from pathlib import Path

from pipeline.broll.slides import collect_slide_jobs, stamp_slide_paths
from pipeline.gemini_director import _DIRECTOR_PROMPT, parse_edit_script, plan_from_transcript
from pipeline.config import Settings
from pipeline.layouts import PictureTag
from pipeline.models import EditScript, GraphicCard, PlannedScene, Scene, TimedTranscript
from pipeline.shotlist import (
    card_is_dense,
    compose_mode,
    local_asset_path,
    overlay_copy_ok,
    resolve_edit_script,
    resolve_scene,
    scene_has_visual,
    scene_shows_slide,
)


def _card(**kwargs: object) -> GraphicCard:
    defaults = {
        "kicker": "THE MONEY",
        "title": "$1.5B is the floor.",
        "icon": "bar_chart",
    }
    defaults.update(kwargs)
    return GraphicCard(**defaults)  # type: ignore[arg-type]


def test_scene_defaults_to_nothing() -> None:
    scene = Scene(start=0, end=10)
    assert scene.said == ""
    assert scene.layout is PictureTag.NOTHING
    assert scene.role == "body"
    assert not scene_has_visual(scene)
    assert not scene_shows_slide(scene)


def test_parse_edit_script_reads_overlay_copy() -> None:
    script = parse_edit_script(
        """
        {
          "scenes": [
            {
              "start": 0,
              "end": 12,
              "said": "Here is the rule.",
              "shown": "overlay card",
              "layout": "overlay",
              "graphic": {
                "kicker": "THE RULE",
                "title": "No empty slides",
                "icon": "shield"
              }
            }
          ]
        }
        """
    )
    scene = script.scenes[0]
    assert scene.said == "Here is the rule."
    assert scene.layout is PictureTag.OVERLAY
    assert overlay_copy_ok(scene.graphic)
    assert scene_has_visual(scene)
    assert not scene_shows_slide(scene)


def test_none_clears_leftover_slide_path() -> None:
    scene = Scene(
        start=0,
        end=4,
        layout=PictureTag.NOTHING,
        graphic=GraphicCard(title="Leftover", asset_path="/tmp/invented.png"),
    )
    resolve_scene(scene)
    assert scene.graphic.asset_path == ""
    assert scene.layout is PictureTag.NOTHING


def test_unknown_asset_kind_coerces_to_none() -> None:
    scene = Scene(start=0, end=4, asset_kind="slide", layout=PictureTag.PIP)
    assert scene.asset_kind == "none"
    resolve_scene(scene)
    assert scene.layout is PictureTag.NOTHING


def test_title_only_card_is_not_overlay() -> None:
    thin = GraphicCard(title="Just a title")
    assert not card_is_dense(thin)
    assert card_is_dense(_card())


def test_title_only_overlay_resolves_to_nothing() -> None:
    scene = Scene(
        start=0,
        end=8,
        layout=PictureTag.OVERLAY,
        said="Let me explain.",
        graphic=GraphicCard(title="Talking points"),
    )
    resolve_scene(scene)
    assert scene.layout is PictureTag.NOTHING
    assert scene.graphic.asset_path == ""
    assert not scene_has_visual(scene)


def test_missing_pip_still_becomes_overlay_or_nothing() -> None:
    scene = Scene(
        start=0,
        end=8,
        layout=PictureTag.PIP,
        asset_ref="broll/does-not-exist.jpg",
        graphic=_card(still_query="MQ-9 Reaper"),
    )
    resolve_scene(scene)
    assert scene.layout is PictureTag.OVERLAY
    assert compose_mode(scene) == "overlay"


def test_missing_site_url_is_nothing() -> None:
    scene = Scene(
        start=0,
        end=8,
        layout=PictureTag.PIP,
        asset_kind="site",
        asset_ref="https://example.com/product",
        graphic=GraphicCard(title="Example", still_query="example"),
    )
    resolve_scene(scene)
    assert scene.layout is PictureTag.NOTHING
    assert local_asset_path("https://example.com/product") is None
    assert compose_mode(scene) == "nothing"


def test_local_still_keeps_pip(tmp_path: Path) -> None:
    still = tmp_path / "mq9-reaper.jpg"
    still.write_bytes(b"not-empty")
    scene = Scene(
        start=0,
        end=8,
        layout=PictureTag.PIP,
        asset_ref=str(still),
        graphic=GraphicCard(kicker="$1.5B", title="in procurements", still_query="MQ-9"),
    )
    resolve_scene(scene)
    assert scene.layout is PictureTag.PIP
    assert scene.asset_ref == str(still.resolve())
    assert compose_mode(scene) == "pip"
    assert not scene_shows_slide(scene)


def test_nothing_scene_does_not_collect_a_slide(tmp_path: Path) -> None:
    script = EditScript(
        scenes=[
            Scene(
                start=0,
                end=12,
                layout=PictureTag.NOTHING,
                graphic=GraphicCard(title="Empty card", slide_id="empty"),
            ),
            Scene(
                start=12,
                end=24,
                layout=PictureTag.OVERLAY,
                graphic=_card(slide_id="real_card"),
            ),
        ]
    )
    resolve_edit_script(script)
    jobs = collect_slide_jobs(script, tmp_path)
    assert jobs == []
    stamp_slide_paths(script, tmp_path)
    assert script.scenes[0].graphic.asset_path == ""
    assert script.scenes[0].layout is PictureTag.NOTHING


def test_director_prompt_is_tag_contract() -> None:
    assert "overlay" in _DIRECTOR_PROMPT
    assert "nothing" in _DIRECTOR_PROMPT
    assert "lower_third" in _DIRECTOR_PROMPT
    assert "Do not generate Scott" in _DIRECTOR_PROMPT
    assert "Never emit layout lower_third" in _DIRECTOR_PROMPT
    assert "open_card" in _DIRECTOR_PROMPT
    assert "Point 1–3 sub talking point" in _DIRECTOR_PROMPT or "Point 1-3" in _DIRECTOR_PROMPT
    assert "TAKEAWAY" in _DIRECTOR_PROMPT
    assert "FULL_FRAME" in _DIRECTOR_PROMPT
    assert "Every scene needs a graphic card" not in _DIRECTOR_PROMPT


def test_overlay_is_the_additive_card() -> None:
    card = Scene(
        start=0,
        end=8,
        layout=PictureTag.OVERLAY,
        graphic=_card(),
    )
    assert compose_mode(card) == "overlay"
    assert not scene_shows_slide(card)


def test_plan_from_transcript_cannot_emit_slide_without_copy(monkeypatch) -> None:
    def fake_generate(client, *, model, contents, schema, temperature=0.4):
        del client, model, contents, temperature
        if schema.__name__ == "_PackagingSchema":
            return {
                "titles": ["A", "B", "C", "D", "E"],
                "description": "Body.",
                "chapters": [
                    {"start": 0, "title": "Open"},
                    {"start": 20, "title": "Middle"},
                    {"start": 40, "title": "Close"},
                ],
                "tags": ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"],
            }
        return {
            "scenes": [
                {
                    "start": 0,
                    "end": 20,
                    "layout": "overlay",
                    "reason": "thin",
                    "said": "Hello there.",
                    "graphic": {"title": "Hello"},
                },
                {
                    "start": 20,
                    "end": 50,
                    "layout": "lower_third",
                    "reason": "model tried a bookend",
                    "graphic": {"title": "Wrap"},
                },
            ],
            "metadata": {},
        }

    monkeypatch.setattr("pipeline.gemini_director._generate_json", fake_generate)
    settings = Settings(gemini_api_key="test", director_chunk_threshold=600)
    transcript = TimedTranscript(duration=50, full_text="Hello there later")
    script = plan_from_transcript(transcript, 50.0, settings, fallback_title="Talk", client=object())
    assert script.scenes
    for scene in script.scenes:
        assert scene.layout in {PictureTag.NOTHING, PictureTag.OVERLAY, PictureTag.PIP}
        assert scene.layout is not PictureTag.LOWER_THIRD


def test_planned_scene_carries_overlay_copy() -> None:
    planned = PlannedScene(
        start=0,
        end=10,
        said="We ship Friday.",
        shown="overlay card",
        graphic=_card(),
        layout=PictureTag.OVERLAY,
    )
    scene = planned.to_scene()
    assert scene.said == "We ship Friday."
    assert scene.layout is PictureTag.OVERLAY
    assert scene_has_visual(scene)
