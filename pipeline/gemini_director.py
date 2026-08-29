from __future__ import annotations

import json
from pathlib import Path

from google import genai
from google.genai import types
from pydantic import ValidationError

from pipeline.config import Settings
from pipeline.media import extract_audio, probe_duration
from pipeline.models import EditScript


class GeminiConfigError(RuntimeError):
    """Missing or invalid Gemini credentials / response."""


DIRECTOR_PROMPT = """You are the edit director for a talking-head YouTube video.

The attached audio is the CURRENT working cut (silence already trimmed).
All timestamps you output MUST be in seconds on this trimmed timeline.
Video duration: {duration:.3f} seconds.

{transcript_block}

Return a single JSON object that matches the schema exactly.

Rules:
- talking_head_cuts: optional extra keep-ranges if the speaker should drop more
  filler after silence trim. Use an empty list to keep the whole cut.
- lower_thirds: clean name/title cards (speaker name, topic, guest). Keep text short.
- broll: moments that need a slide, screen, or B-roll insert. Prefer "pip" unless
  a full-frame cutaway is clearly better ("fade" or "cut"). Put a search phrase
  in query. Leave asset_path null.
- overlays: takeaway / stat / quote callouts, not name cards.
- metadata.titles: exactly 5 distinct high-CTR YouTube titles.
- metadata.description: SEO description with a short hook, value, and CTA.
- metadata.chapters: chapter markers on this same timeline.
- transcript: full cleaned transcript of the attached audio.
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
    prompt = DIRECTOR_PROMPT.format(
        duration=duration,
        transcript_block=_transcript_block(transcript),
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
        return parsed

    text = (getattr(response, "text", None) or "").strip()
    if not text:
        raise GeminiConfigError("Gemini returned an empty edit script.")
    return parse_edit_script(text)


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
    return "Human-provided transcript (use as a guide, correct timestamps from audio):\n" + transcript.strip()
