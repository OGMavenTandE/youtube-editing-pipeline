from pathlib import Path

from pipeline.broll.slides import collect_slide_jobs, stamp_slide_paths
from pipeline.gemini_director import _DIRECTOR_PROMPT, parse_edit_script, plan_from_transcript
from pipeline.config import Settings
from pipeline.layouts import LayoutKind
from pipeline.models import EditScript, GraphicCard, PlannedScene, Scene, TimedTranscript
from pipeline.shotlist import (
    card_is_dense,
    local_asset_path,
    resolve_edit_script,
    resolve_scene,
    scene_has_visual,
    scene_shows_slide,
)


def _dense_card(**kwargs: object) -> GraphicCard:
    defaults = {
        "kicker": "Policy",
        "title": "The number that matters",
        "bullets": ["Cut the pause", "Keep the point"],
    }
    defaults.update(kwargs)
    return GraphicCard(**defaults)  # type: ignore[arg-type]


def test_scene_shot_list_defaults_to_none() -> None:
    scene = Scene(start=0, end=10)
    assert scene.said == ""
    assert scene.shown == ""
    assert scene.asset_kind == "none"
    assert scene.asset_ref is None
    assert not scene_has_visual(scene)
    assert not scene_shows_slide(scene)


def test_parse_edit_script_reads_shot_list_fields() -> None:
    script = parse_edit_script(
        """
        {
          "scenes": [
            {
              "start": 0,
              "end": 12,
              "said": "Here is the rule.",
              "shown": "dense card on the claim",
              "asset_kind": "card",
              "asset_ref": null,
              "layout": "PIP_BOTTOM_RIGHT",
              "graphic": {
                "kicker": "Rule",
                "title": "No empty slides",
                "bullets": ["Name the artifact", "Or stay on camera"]
              }
            }
          ]
        }
        """
    )
    scene = script.scenes[0]
    assert scene.said == "Here is the rule."
    assert scene.shown == "dense card on the claim"
    assert scene.asset_kind == "card"
    assert scene_has_visual(scene)
    assert scene_shows_slide(scene)


def test_none_clears_leftover_slide_path() -> None:
    scene = Scene(
        start=0,
        end=4,
        asset_kind="none",
        layout=LayoutKind.PIP_BOTTOM_RIGHT,
        graphic=GraphicCard(title="Leftover", asset_path="/tmp/invented.png"),
    )
    resolve_scene(scene)
    assert scene.graphic.asset_path == ""
    assert scene.layout is LayoutKind.FULL_FRAME


def test_unknown_asset_kind_coerces_to_none() -> None:
    scene = Scene(start=0, end=4, asset_kind="slide", layout=LayoutKind.PIP_BOTTOM_RIGHT)
    assert scene.asset_kind == "none"
    resolve_scene(scene)
    assert scene.asset_kind == "none"
    assert scene.layout is LayoutKind.FULL_FRAME


def test_title_only_card_is_not_dense() -> None:
    thin = GraphicCard(title="Just a title")
    assert not card_is_dense(thin)
    assert card_is_dense(_dense_card())


def test_title_only_card_resolves_to_talking_head() -> None:
    scene = Scene(
        start=0,
        end=8,
        layout=LayoutKind.PIP_BOTTOM_RIGHT,
        asset_kind="card",
        said="Let me explain.",
        shown="empty title slide",
        graphic=GraphicCard(title="Talking points", slide_id="thin"),
    )
    resolve_scene(scene)
    assert scene.asset_kind == "none"
    assert scene.layout is LayoutKind.FULL_FRAME
    assert scene.graphic.asset_path == ""
    assert not scene_has_visual(scene)
    assert not scene_shows_slide(scene)


def test_missing_broll_file_is_none_not_invented_slide() -> None:
    scene = Scene(
        start=0,
        end=8,
        layout=LayoutKind.PIP_BOTTOM_RIGHT,
        asset_kind="broll",
        asset_ref="broll/does-not-exist.mp4",
        graphic=GraphicCard(title="Invented"),
    )
    resolve_scene(scene)
    assert scene.asset_kind == "none"
    assert scene.layout is LayoutKind.FULL_FRAME
    assert scene.graphic.asset_path == ""


def test_missing_site_url_is_none(tmp_path: Path) -> None:
    scene = Scene(
        start=0,
        end=8,
        layout=LayoutKind.SPLIT_TOP,
        asset_kind="site",
        asset_ref="https://example.com/product",
        graphic=GraphicCard(title="Example"),
    )
    resolve_scene(scene)
    assert scene.asset_kind == "none"
    assert local_asset_path("https://example.com/product") is None
    assert not scene_has_visual(scene)


def test_local_broll_path_is_kept(tmp_path: Path) -> None:
    clip = tmp_path / "shop-floor.mp4"
    clip.write_bytes(b"not-empty")
    scene = Scene(
        start=0,
        end=8,
        layout=LayoutKind.PIP_BOTTOM_RIGHT,
        asset_kind="broll",
        asset_ref=str(clip),
        graphic=GraphicCard(title="Shop floor"),
    )
    resolve_scene(scene)
    assert scene.asset_kind == "broll"
    assert scene.asset_ref == str(clip.resolve())
    assert scene.graphic.asset_path == str(clip.resolve())
    assert scene_has_visual(scene)
    assert not scene_shows_slide(scene)


def test_none_scene_does_not_collect_a_slide(tmp_path: Path) -> None:
    script = EditScript(
        scenes=[
            Scene(
                start=0,
                end=12,
                layout=LayoutKind.PIP_BOTTOM_RIGHT,
                asset_kind="none",
                graphic=GraphicCard(title="Empty card", bullets=["Nope"], slide_id="empty"),
            ),
            Scene(
                start=12,
                end=24,
                layout=LayoutKind.PIP_BOTTOM_RIGHT,
                asset_kind="card",
                graphic=_dense_card(slide_id="real_card"),
            ),
        ]
    )
    resolve_edit_script(script)
    jobs = collect_slide_jobs(script, tmp_path)
    variants = {job.slide_id for job in jobs}
    assert "empty" not in variants
    assert "real_card" in variants
    stamp_slide_paths(script, tmp_path)
    assert script.scenes[0].graphic.asset_path == ""
    assert script.scenes[0].layout is LayoutKind.FULL_FRAME
    assert script.scenes[1].graphic.asset_path.endswith("real_card_pip.png")


def test_director_prompt_is_producer_not_layout_roulette() -> None:
    assert "SAID" in _DIRECTOR_PROMPT.upper() or "said:" in _DIRECTOR_PROMPT
    assert "shown:" in _DIRECTOR_PROMPT
    assert "asset_kind" in _DIRECTOR_PROMPT
    assert "none" in _DIRECTOR_PROMPT
    assert "Nate-style" in _DIRECTOR_PROMPT or "kicker" in _DIRECTOR_PROMPT
    assert "Never use the same layout three times" not in _DIRECTOR_PROMPT
    assert "Every scene needs a graphic card" not in _DIRECTOR_PROMPT


def test_plan_from_transcript_cannot_emit_slide_without_asset(monkeypatch) -> None:
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
                    "layout": "PIP_BOTTOM_RIGHT",
                    "reason": "variety",
                    "said": "Hello there.",
                    "shown": "empty title card",
                    "asset_kind": "card",
                    "graphic": {"title": "Hello", "slide_id": "thin"},
                },
                {
                    "start": 20,
                    "end": 50,
                    "layout": "SPLIT_TOP",
                    "reason": "still empty",
                    "asset_kind": "none",
                    "graphic": {"title": "Aside"},
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
        if scene.asset_kind == "none":
            assert scene.layout is LayoutKind.FULL_FRAME
            assert not scene_shows_slide(scene)
            assert scene.graphic.asset_path == ""
        else:
            assert scene.asset_kind in {"card", "broll", "site"}
            assert scene_has_visual(scene)
    assert all(scene.asset_kind == "none" for scene in script.scenes)


def test_planned_scene_carries_shot_list() -> None:
    planned = PlannedScene(
        start=0,
        end=10,
        said="We ship Friday.",
        shown="dense card",
        asset_kind="card",
        graphic=_dense_card(),
        layout=LayoutKind.PIP_BOTTOM_RIGHT,
    )
    scene = planned.to_scene()
    assert scene.said == "We ship Friday."
    assert scene.asset_kind == "card"
    assert scene_has_visual(scene)
