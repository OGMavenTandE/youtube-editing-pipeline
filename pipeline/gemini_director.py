"""Two-pass Gemini director: audio transcript, then text-only scene plan."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types
from pydantic import BaseModel, Field, ValidationError

from pipeline.config import Settings
from pipeline.layouts import LayoutKind
from pipeline.media import extract_audio, probe_duration
from pipeline.models import (
    DirectorPlan,
    EditScript,
    GraphicCard,
    PlannedScene,
    TimedTranscript,
    TranscriptCue,
    YouTubeMetadata,
)
from pipeline.pacing import expected_scene_range

logger = logging.getLogger(__name__)

# Gemini inline media limit. 16 kHz mono WAV hits this around 11 minutes.
INLINE_AUDIO_LIMIT_BYTES = 20 * 1024 * 1024

_ALLOWED_LAYOUTS = {item.value for item in LayoutKind}

_GENERIC_TAGS = (
    "talking head",
    "tutorial",
    "walkthrough",
    "explainer",
    "how to",
    "guide",
    "lesson",
    "breakdown",
    "tips",
    "youtube",
)


class GeminiConfigError(RuntimeError):
    """Missing or invalid Gemini credentials / response."""


class _TranscriptSchema(BaseModel):
    text: str = ""
    cues: list[TranscriptCue] = Field(default_factory=list)


_TRANSCRIPT_PROMPT = """\
Transcribe this talking-head audio.

Return a JSON object with:
- "text": the full transcript as one string
- "cues": an array of { "start": seconds, "end": seconds, "text": spoken words }

Cues should follow natural sentence or clause boundaries. Cover the whole file.
Do not summarize. Do not invent words that were not spoken.
"""

_DIRECTOR_PROMPT = """\
You are the director for a talking-head YouTube edit.

You receive a timed transcript of the trimmed video. The webcam is a static
landscape talking-head shot. Do not request video frames.

A scene is a visual beat, usually 8 to 25 seconds. Plan scenes that cover
the assigned window with no gaps and no overlaps. Do not emit one scene for
the whole file. Do not emit micro-events (punch-in, text flash, extra cuts).
Those are added later in code.

Use absolute timestamps in seconds on the full video timeline.

Layouts (pick from content, never rotate A-B-C):
- FULL_FRAME: webcam fills the frame. Default for stories, asides, transitions.
- PIP_BOTTOM_RIGHT: generated slide fills the frame, webcam as a lower-right
  bubble. Use when a list, definition, number, or named idea is on screen.
- SPLIT_TOP: webcam on top two-thirds, graphic on the bottom third. Use for
  one big claim, a quote, or a named concept.

Never use the same layout three times in a row.
Every scene needs a short reason (why this layout here).
Every scene needs a graphic card:
- title: 3 to 8 words
- bullets: 0 to 3 short lines
- lower_third_title / lower_third_subtitle: optional name lines
- slide_id: stable id like slide_001

YouTube metadata (only when asked for this window):
- titles: exactly 5 options, each a different angle (how-to, result, tension,
  named concept, curiosity). Do not write five near-duplicates.
- description: hook in the first two lines, then body, then a chapter list.
- chapters: first chapter starts at exactly 0. At least 3 chapters. Each
  chapter is at least 10 seconds. Prefer a new chapter every 90 to 180 seconds
  on a real topic shift, not every sentence.
- tags: 10 to 15 search terms.

Return JSON with only "scenes" and "metadata".
"""


def _require_key(settings: Settings) -> None:
    if not settings.gemini_api_key:
        raise GeminiConfigError(
            "GEMINI_API_KEY is not set. Copy .env.example to .env and add a "
            "Google AI Studio key. Do not commit the key."
        )


def _client(settings: Settings) -> genai.Client:
    _require_key(settings)
    return genai.Client(api_key=settings.gemini_api_key)


def _audio_part(client: genai.Client, audio_path: Path) -> Any:
    size = audio_path.stat().st_size
    mime = "audio/mpeg" if audio_path.suffix.lower() == ".mp3" else "audio/wav"
    if size <= INLINE_AUDIO_LIMIT_BYTES:
        logger.info("Uploading audio inline (%s bytes)", size)
        return types.Part.from_bytes(data=audio_path.read_bytes(), mime_type=mime)
    logger.info("Uploading audio via Files API (%s bytes)", size)
    uploaded = client.files.upload(file=str(audio_path))
    return uploaded


def _generate_json(
    client: genai.Client,
    *,
    model: str,
    contents: list[Any],
    schema: type[BaseModel],
    temperature: float = 0.4,
) -> dict[str, Any]:
    try:
        response = client.models.generate_content(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(
                temperature=temperature,
                response_mime_type="application/json",
                response_schema=schema,
            ),
        )
    except GeminiConfigError:
        raise
    except Exception as exc:
        raise GeminiConfigError(f"Gemini request failed: {exc}") from exc

    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, schema):
        return parsed.model_dump()
    raw = (getattr(response, "text", None) or "").strip()
    if not raw:
        raise GeminiConfigError("Gemini returned an empty JSON response.")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GeminiConfigError(f"Gemini returned invalid JSON: {exc}") from exc


def director_windows(duration: float, settings: Settings) -> list[tuple[float, float]]:
    """One window if under the threshold, else adjacent director_chunk_seconds slices."""
    duration = max(0.0, duration)
    if duration <= 0:
        return [(0.0, 0.0)]
    if duration <= settings.director_chunk_threshold:
        return [(0.0, duration)]
    step = settings.director_chunk_seconds
    windows: list[tuple[float, float]] = []
    start = 0.0
    while start < duration - 0.01:
        end = min(start + step, duration)
        windows.append((start, end))
        start = end
    return windows


def _shift_scenes(scenes: list[PlannedScene], offset: float) -> list[PlannedScene]:
    if abs(offset) < 1e-9:
        return list(scenes)
    shifted: list[PlannedScene] = []
    for scene in scenes:
        child = scene.model_copy(deep=True)
        child.start = scene.start + offset
        child.end = scene.end + offset
        shifted.append(child)
    return shifted


def fit_scenes_to_window(
    scenes: list[PlannedScene],
    window_start: float,
    window_end: float,
) -> list[PlannedScene]:
    """Accept absolute or window-relative timestamps, then clamp to the window."""
    if not scenes:
        return []
    span = max(0.0, window_end - window_start)
    in_abs = sum(
        1
        for scene in scenes
        if scene.start >= window_start - 1.0 and scene.start < window_end + 1.0
    )
    in_rel = sum(1 for scene in scenes if -0.5 <= scene.start <= span + 1.0)
    if (
        window_start > 0.5
        and in_abs < max(1, len(scenes) / 2)
        and in_rel >= max(1, len(scenes) / 2)
    ):
        scenes = _shift_scenes(scenes, window_start)

    fitted: list[PlannedScene] = []
    for scene in scenes:
        start = max(window_start, min(scene.start, window_end))
        end = max(start, min(scene.end, window_end))
        if end - start < 0.05:
            continue
        child = scene.model_copy(deep=True)
        child.start = start
        child.end = end
        if child.layout.value not in _ALLOWED_LAYOUTS:
            child.layout = LayoutKind.FULL_FRAME
        fitted.append(child)
    return fitted


def stitch_director_plans(
    plans: list[DirectorPlan],
    windows: list[tuple[float, float]],
    duration: float,
) -> DirectorPlan:
    scenes: list[PlannedScene] = []
    metadata = plans[0].metadata if plans else YouTubeMetadata()
    for plan, (start, end) in zip(plans, windows):
        scenes.extend(fit_scenes_to_window(plan.scenes, start, end))
    scenes.sort(key=lambda item: item.start)
    if not scenes and duration > 0:
        scenes = [
            PlannedScene(
                start=0.0,
                end=duration,
                layout=LayoutKind.FULL_FRAME,
                reason="Fallback: director returned no scenes",
                graphic=GraphicCard(title="Talking head", slide_id="slide_001"),
            )
        ]
    return DirectorPlan(scenes=scenes, metadata=metadata)


def normalize_youtube_metadata(
    metadata: YouTubeMetadata,
    duration: float,
    *,
    fallback_title: str,
) -> YouTubeMetadata:
    """Force chapter, title, and tag rules the model often misses."""
    from pipeline.models import ChapterMarker

    titles = [title.strip() for title in metadata.titles if title.strip()]
    seen: set[str] = set()
    unique: list[str] = []
    for title in titles:
        key = title.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(title)
    while len(unique) < 5:
        suffix = f" ({len(unique) + 1})" if unique else ""
        candidate = f"{fallback_title}{suffix}".strip() or f"Untitled ({len(unique) + 1})"
        if candidate.casefold() not in seen:
            unique.append(candidate)
            seen.add(candidate.casefold())
        else:
            unique.append(f"{fallback_title} ({len(unique) + 1})")
    unique = unique[:5]

    description = (metadata.description or "").strip()
    if not description:
        description = f"{unique[0]}\n\nChapters below."

    chapters = sorted(
        [chapter for chapter in metadata.chapters if chapter.title.strip()],
        key=lambda item: item.start,
    )
    if not chapters or chapters[0].start > 0.05:
        intro = chapters[0].title if chapters else "Intro"
        chapters = [ChapterMarker(start=0.0, title=intro), *chapters]
    chapters[0].start = 0.0

    merged = [chapters[0]]
    for chapter in chapters[1:]:
        prev = merged[-1]
        if chapter.start - prev.start < 10:
            continue
        if chapter.start >= duration:
            continue
        merged.append(chapter)
    chapters = merged

    if len(chapters) < 3 and duration >= 30:
        labels = [chapter.title for chapter in chapters]
        while len(labels) < 3:
            labels.append(f"Part {len(labels) + 1}")
        third = duration / 3.0
        chapters = [
            ChapterMarker(start=0.0, title=labels[0]),
            ChapterMarker(start=max(10.0, third), title=labels[1]),
            ChapterMarker(start=max(20.0, min(duration - 0.01, third * 2)), title=labels[2]),
        ]

    tags = [tag.strip() for tag in metadata.tags if tag.strip()]
    seen_tags: set[str] = set()
    unique_tags: list[str] = []
    for tag in tags:
        key = tag.casefold()
        if key in seen_tags:
            continue
        seen_tags.add(key)
        unique_tags.append(tag)

    extras = [word.lower() for word in fallback_title.replace("-", " ").split() if len(word) > 2]
    extras.extend(_GENERIC_TAGS)
    for extra in extras:
        if len(unique_tags) >= 10:
            break
        if extra.casefold() not in seen_tags:
            unique_tags.append(extra)
            seen_tags.add(extra.casefold())
    unique_tags = unique_tags[:15]
    return YouTubeMetadata(
        titles=unique,
        description=description,
        chapters=chapters,
        tags=unique_tags,
    )


def _director_user_text(
    transcript: TimedTranscript,
    settings: Settings,
    *,
    start: float,
    end: float,
    duration: float,
    window_index: int,
    window_count: int,
    include_metadata: bool,
) -> str:
    lo, hi = expected_scene_range(end - start, settings)
    parts = [
        f"Full video duration: {duration:.2f} seconds.",
        f"Plan scenes for this window only: {start:.2f}s to {end:.2f}s.",
        "Use absolute timestamps on the full timeline.",
        f"Aim for about {lo} to {hi} scenes in this window. Cover the window with no gaps.",
    ]
    if window_count > 1:
        parts.append(f"This is window {window_index + 1} of {window_count}.")
    if include_metadata:
        parts.append(
            "Also return metadata for the whole video (5 titles, description, "
            "chapters from 0.0, 10-15 tags)."
        )
        if transcript.text:
            parts.append(f"\nFull transcript (for titles, description, chapters):\n{transcript.text}")
    else:
        parts.append("Omit metadata. Return empty titles, chapters, and tags.")
    parts.append(f"\nTimed transcript for this window:\n{transcript.window_text(start, end)}")
    return "\n".join(parts)


def transcribe_audio(
    audio_path: Path,
    settings: Settings,
    *,
    duration: float | None = None,
    client: genai.Client | None = None,
) -> TimedTranscript:
    """Pass 1: audio only. Persist the result so the director can be re-run."""
    if not audio_path.is_file():
        raise FileNotFoundError(f"Audio not found: {audio_path}")
    api = client or _client(settings)
    payload = _generate_json(
        api,
        model=settings.gemini_model,
        contents=[
            types.Content(
                role="user",
                parts=[
                    types.Part.from_text(text=_TRANSCRIPT_PROMPT),
                    _audio_part(api, audio_path),
                ],
            )
        ],
        schema=_TranscriptSchema,
        temperature=0.2,
    )
    parsed = _TranscriptSchema.model_validate(payload)
    transcript = TimedTranscript(
        duration=duration or 0.0,
        full_text=parsed.text.strip(),
        cues=parsed.cues,
    )
    if not transcript.text:
        raise GeminiConfigError("Gemini returned an empty transcript.")
    logger.info("Transcript: %s chars, %s cues", len(transcript.text), len(transcript.cues))
    return transcript


def plan_from_transcript(
    transcript: TimedTranscript,
    duration: float,
    settings: Settings,
    *,
    fallback_title: str,
    client: genai.Client | None = None,
) -> EditScript:
    """Pass 2: text transcript in, scenes + metadata out. No audio re-upload."""
    api = client or _client(settings)
    windows = director_windows(duration, settings)
    logger.info("Director windows: %s", windows)
    plans: list[DirectorPlan] = []
    for index, (start, end) in enumerate(windows):
        logger.info(
            "Director window %s/%s: %.1f-%.1f",
            index + 1,
            len(windows),
            start,
            end,
        )
        include_metadata = index == 0
        payload = _generate_json(
            api,
            model=settings.gemini_model,
            contents=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_text(text=_DIRECTOR_PROMPT),
                        types.Part.from_text(
                            text=_director_user_text(
                                transcript,
                                settings,
                                start=start,
                                end=end,
                                duration=duration,
                                window_index=index,
                                window_count=len(windows),
                                include_metadata=include_metadata,
                            )
                        ),
                    ],
                )
            ],
            schema=DirectorPlan,
            temperature=0.4,
        )
        plan = DirectorPlan.model_validate(payload)
        if not include_metadata:
            plan.metadata = YouTubeMetadata()
        plans.append(plan)

    stitched = stitch_director_plans(plans, windows, duration)
    metadata = normalize_youtube_metadata(
        stitched.metadata,
        duration,
        fallback_title=fallback_title,
    )
    return EditScript(
        transcript=transcript.text,
        talking_head_cuts=[],
        scenes=[scene.to_scene() for scene in stitched.scenes],
        metadata=metadata,
    )


def transcript_to_payload(transcript: TimedTranscript) -> dict[str, Any]:
    payload = transcript.model_dump()
    payload["text"] = transcript.text
    return payload


def save_transcript(transcript: TimedTranscript, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(transcript_to_payload(transcript), indent=2) + "\n", encoding="utf-8")
    return path


def load_transcript(path: Path, *, duration: float = 0.0) -> TimedTranscript:
    raw = path.read_text(encoding="utf-8")
    return parse_transcript(raw, duration=duration)


def parse_transcript(raw: str, *, duration: float = 0.0) -> TimedTranscript:
    """JSON transcript, or a plain-text fallback."""
    cleaned = raw.strip()
    if cleaned.startswith("{"):
        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and (
            "cues" in payload or "full_text" in payload or "text" in payload
        ):
            transcript = TimedTranscript.model_validate(payload)
            if transcript.duration <= 0 and duration > 0:
                transcript.duration = duration
            return transcript
    return TimedTranscript.from_plain(cleaned, duration)


def analyze_video(
    video_path: Path,
    settings: Settings,
    *,
    transcript: str | None = None,
    duration: float | None = None,
    transcript_path: Path | None = None,
    transcript_out: Path | None = None,
) -> EditScript:
    """Two-pass director: transcribe trimmed audio, then plan scenes from text."""
    _require_key(settings)
    video_path = video_path.resolve()
    if not video_path.is_file():
        raise FileNotFoundError(f"Video not found: {video_path}")

    settings.ensure_dirs()
    duration = duration if duration is not None else probe_duration(video_path, settings)
    fallback_title = video_path.stem.replace("_", " ").replace("-", " ").strip() or "Untitled"

    reused: TimedTranscript | None = None
    if transcript_path is not None:
        transcript_path = transcript_path.expanduser()
        if not transcript_path.is_file():
            raise FileNotFoundError(f"Transcript not found: {transcript_path}")
        reused = load_transcript(transcript_path, duration=duration)
        logger.info("Reusing transcript %s", transcript_path)
    elif transcript and transcript.strip():
        reused = parse_transcript(transcript, duration=duration)

    if reused is not None and reused.text:
        timed = reused
        if timed.duration <= 0:
            timed.duration = duration
    else:
        audio_path = settings.work_dir / f"{video_path.stem}_gemini.wav"
        extract_audio(video_path, audio_path, settings)
        timed = transcribe_audio(audio_path, settings, duration=duration)

    if transcript_out:
        save_transcript(timed, transcript_out)
        logger.info("Wrote transcript %s", transcript_out)

    return plan_from_transcript(
        timed,
        duration,
        settings,
        fallback_title=fallback_title,
    )


def parse_edit_script(raw: str) -> EditScript:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
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
