from pipeline.gemini_director import parse_edit_script
from pipeline.models import EditScript, SilenceCutMap, TimeRange


def test_edit_script_parses_director_json() -> None:
    raw = """
    {
      "transcript": "Hello there.",
      "talking_head_cuts": [{"start": 0, "end": 4, "reason": "intro"}],
      "lower_thirds": [{"start": 0.5, "end": 3.0, "title": "Host", "subtitle": "Channel"}],
      "broll": [{"start": 2.0, "end": 3.5, "query": "city night", "transition": "pip"}],
      "overlays": [{"start": 1.0, "end": 2.5, "text": "Start with the hook", "kind": "takeaway"}],
      "metadata": {
        "titles": ["One", "Two", "Three", "Four", "Five", "Six"],
        "description": "A tight cut.",
        "chapters": [{"start": 0, "title": "Intro"}],
        "tags": ["edit"]
      }
    }
    """
    script = parse_edit_script(raw)
    assert isinstance(script, EditScript)
    assert len(script.metadata.titles) == 5
    assert script.lower_thirds[0].title == "Host"
    assert script.broll[0].transition == "pip"


def test_fenced_json_is_accepted() -> None:
    script = parse_edit_script("```json\n{\"transcript\": \"hi\", \"metadata\": {\"titles\": [\"A\"]}}\n```")
    assert script.transcript == "hi"
    assert script.metadata.titles == ["A"]


def test_cut_map_maps_kept_time() -> None:
    cut_map = SilenceCutMap(
        kept_ranges=[TimeRange(start=0, end=2), TimeRange(start=5, end=7)],
        removed_ranges=[TimeRange(start=2, end=5)],
        original_duration=7,
        trimmed_duration=4,
    )
    assert cut_map.to_trimmed(1.0) == 1.0
    assert cut_map.to_trimmed(5.5) == 2.5
    assert cut_map.to_trimmed(3.0) is None
