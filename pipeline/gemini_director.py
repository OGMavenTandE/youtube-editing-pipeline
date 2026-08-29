from __future__ import annotations

import json
from pathlib import Path

from google import genai
from google.genai import types
from pydantic import ValidationError

from pipeline.config import Settings
from pipeline.media import extract_audio, probe_duration
from pipeline.models import EditScript
from pipeline.pacing import enforce_pacing, expected_scene_range


class GeminiConfigError(RuntimeError):
    """Missing or invalid Gemini credentials / response."""


DIRECTOR_PROMPT = """You are the edit director for a landscape talking-head YouTube video.

The attached audio is the CURRENT working cut (silence already trimmed).
All timestamps MUST be seconds on this trimmed timeline.
Video duration: {duration:.3f} seconds ({minutes:.1f} minutes).

{transcript_block}

Return one JSON object that matches the schema.

SCENE LIST (required)
A scene is a short layout beat, NOT the whole video.
A {minutes:.0f}-minute cut MUST have about {scene_low}-{scene_high} scenes, not 3.
Cover 0.000 through {duration:.3f} with contiguous scenes (tiny gaps only).

LAYOUT HOLDS (heavy change: FULL_FRAME / PIP_BOTTOM_RIGHT / SPLIT_TOP)
- First {hook:.0f}s: hold 8-15s (target ~12s). Open FULL_FRAME, then switch when the first concrete idea is named.
- After that: hold 15-25s (target ~20s).
- Hard floor 8s unless the remaining tail is shorter.
- Hard ceiling 40s on the same layout. Prefer staying under 25s after the hook.
- Do NOT rotate in a fixed A-B-C order. Pick from the spoken line:
  - hook, story, punchline, CTA, direct address -> FULL_FRAME
  - list, definition, framework, "look at this" -> PIP_BOTTOM_RIGHT
  - still talking, one supporting point on screen -> SPLIT_TOP
- Ban the same layout three times in a row unless it is one continuous story, and even then split the hold.

MICRO-RESETS (light change inside a hold)
These are NOT layout swaps. Put them on scene.micro_events:
- punch_in: digital zoom ~1.15x on a keyword (1.2-1.8s)
- text: short takeaway / stat / quote
- cut: hard cut at the scene boundary (you can emit these; the compositor also treats scene edges as cuts)
Target a visual reset every 5-7s (every ~6s). In the first 60s, bias toward 3-5s light resets
(punch-in or text) on top of the tighter layout holds.

SLIDES
New slide (graphic.title / bullets / slide_id) only when the topic changes.
One slide_id may cover two or three adjacent PIP/SPLIT scenes.
Keep bullets sparse (max 3). Do not put a new slide on every sentence.

METADATA
- metadata.titles: exactly 5 distinct high-CTR titles
- metadata.description: SEO description with hook, value, CTA
- metadata.chapters: chapter markers on this timeline
- transcript: full cleaned transcript

Also fill talking_head_cuts only if more filler should drop after silence trim. Empty means keep all.
"""


def analyze_video(
    video_path: Path,
    settings: Settings,
    *,
    transcript: str | None = None,
    duration: float | None = None,
) -> EditScript:
    """Send audio (and optional transcript) to Gemini 2.5 Flash; validate JSON."""
    if not settings.gemini_api_key:
        raise GeminiConfigError(
            "GEMINI_API_KEY is not set. Copy .env.example to .env and add a "
            "Google AI Studio key. Do not commit the key."
        )

    video_path = video_path.resolve()
    if not video_path.is_file():
        raise FileNotFoundError(f"Video not found: {video_path}")

    settings.ensure_dirs()
    duration = duration if duration is not None else probe_duration(video_path, settings)
    audio_path = settings.work_dir / f"{video_path.stem}_gemini.wav"
    extract_audio(video_path, audio_path, settings)

    client = genai.Client(api_key=settings.gemini_api_key)
    uploaded = client.files.upload(file=str(audio_path))
    scene_low, scene_high = expected_scene_range(duration, settings)
    prompt = DIRECTOR_PROMPT.format(
        duration=duration,
        minutes=duration / 60.0,
        transcript_block=_transcript_block(transcript),
        scene_low=scene_low,
        scene_high=scene_high,
        hook=settings.pacing_hook_window,
    )

    try:
        response = client.models.generate_content(
            model=settings.gemini_model,
            contents=[prompt, uploaded],
            config=types.GenerateContentConfig(
                temperature=0.4,
                response_mime_type="application/json",
                response_schema=EditScript,
            ),
        )
    except Exception as exc:
        raise GeminiConfigError(f"Gemini request failed: {exc}") from exc
    finally:
        if audio_path.exists():
            audio_path.unlink()

    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, EditScript):
        return enforce_pacing(parsed, duration, settings)

    text = (getattr(response, "text", None) or "").strip()
    if not text:
        raise GeminiConfigError("Gemini returned an empty edit script.")
    return enforce_pacing(parse_edit_script(text), duration, settings)


def parse_edit_script(raw: str) -> EditScript:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned[: -3]
        cleaned = cleaned.strip()
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
    try:
        return EditScript.model_validate_json(cleaned)
    except ValidationError:
        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise GeminiConfigError(f"Gemini JSON was not valid: {exc}") from exc
        try:
            return EditScript.model_validate(payload)
        except ValidationError as exc:
            raise GeminiConfigError(f"Gemini JSON failed schema validation: {exc}") from exc


def load_edit_script(path: Path) -> EditScript:
    return parse_edit_script(path.read_text(encoding="utf-8"))


def _transcript_block(transcript: str | None) -> str:
    if not transcript or not transcript.strip():
        return "No precomputed transcript was provided. Transcribe from the audio."
    return (
        "Human-provided transcript (use as a guide, correct timestamps from audio):\n"
        + transcript.strip()
    )
