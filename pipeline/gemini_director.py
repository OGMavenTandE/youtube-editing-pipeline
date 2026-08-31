"""Two-pass Gemini director: audio transcript, then text-only scene plan."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types
from pydantic import BaseModel, Field, ValidationError

from pipeline.config import Settings
from pipeline.layouts import BODY_TAGS, PictureTag
from pipeline.media import MediaError, extract_audio, extract_compact_audio, probe_duration
from pipeline.models import (
    ChapterMarker,
    DirectorPlan,
    EditScript,
    GraphicCard,
    PlannedScene,
    TimedTranscript,
    TranscriptCue,
    YouTubeMetadata,
)
from pipeline.pacing import expected_scene_range
from pipeline.shotlist import resolve_edit_script

logger = logging.getLogger(__name__)

# Gemini inline media limit. 16 kHz mono WAV hits this around 11 minutes.
INLINE_AUDIO_LIMIT_BYTES = 20 * 1024 * 1024

_ALLOWED_LAYOUTS = set(BODY_TAGS)

GENERIC_TAGS = (
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
GENERIC_FILLER_TAGS = frozenset(tag.casefold() for tag in GENERIC_TAGS)


class GeminiConfigError(RuntimeError):
    """Missing or invalid Gemini credentials / response."""


class _TranscriptSchema(BaseModel):
    text: str = ""
    cues: list[TranscriptCue] = Field(default_factory=list)


class _PackagingSchema(BaseModel):
    """Gemini packaging payload. title_index is a local desk pick, not model output."""

    titles: list[str] = Field(default_factory=list)
    description: str = ""
    chapters: list[ChapterMarker] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


_TRANSCRIPT_PROMPT = """\
Transcribe this talking-head audio.

Return a JSON object with:
- "text": the full transcript as one string
- "cues": an array of { "start": seconds, "end": seconds, "text": spoken words }

Cues should follow natural sentence or clause boundaries. Cover the whole file.
Do not summarize. Do not invent words that were not spoken.
"""

_DIRECTOR_PROMPT = """\
You tag spoken beats for a locked Scott Mastin YouTube picture kit.

You receive a timed transcript. The webcam is a static landscape talking-head
shot of Scott. Do not request video frames. Do not generate Scott. Do not
invent a look, layout, font, color, zoom, or slide.

The app already owns open and close bookends (identity lower-third + a card).
You tag BODY beats only.

Talk structure Scott will have already followed:
- Title + executive summary (spoken) — app bookend, skip this window
- Point 1, with 2–3 spoken subpoints
- Point 2, same
- Point 3, same
- Closing wrap + contact — app bookend, skip this window

Allowed layout values (this is a tag, not a design choice):
- nothing: default. Full-frame host, no chrome. Most beats are this.
- overlay: a spoken subpoint that deserves the locked Nate card.
  graphic.kicker = short gold eyebrow (all-caps idea, e.g. THE MONEY)
  graphic.title = 1–2 line white headline he actually said
  graphic.icon = one of bar_chart | robot | shield | drone | share | chip | lock | target
- pip: rare. Only when a named-platform still is the point (DVIDS 16:9).
  graphic.kicker = gold number or short kicker
  graphic.title = white sub line
  graphic.quote = optional short quote
  graphic.still_query = the named platform (e.g. MQ-9 Reaper, M1 Abrams)

Rules:
- Sparse. Do not tag every 3–6 seconds. A 20-minute cut might have 6–12 overlays
  and 0–2 pip beats. The rest is nothing.
- Never emit layout lower_third, FULL_FRAME, PIP_BOTTOM_RIGHT, or SPLIT_TOP.
- Never invent a browser, HUD, TAKEAWAY pill, or generated host.
- If you cannot name a real DVIDS still, do not use pip. Use overlay or nothing.
- Cover the assigned window with no gaps. Consecutive nothing beats are correct.
- said: short quote from the transcript. shown: webcam / card / named still.

Do not write YouTube titles, description, chapters, or tags here.
Return empty metadata. A later pass writes packaging for the full cut.

Return JSON with only "scenes" and "metadata".
"""

_METADATA_PROMPT = """\
You write YouTube packaging for a finished talking-head cut.

You receive the FULL transcript and the FULL duration of the trimmed video.
Write titles, description, chapters, and tags for the whole cut, not one
window.

Rules:
- titles: exactly 5 options, each a different angle (how-to, result, tension,
  named concept, curiosity). Do not write five near-duplicates.
- description: hook in the first two lines, then body. You may include a
  chapter list; a later step rewrites one canonical block.
- chapters: first chapter starts at exactly 0. At least 3 chapters. Each
  chapter is at least 10 seconds. Cover the entire duration. Prefer a new
  chapter every 90 to 180 seconds on a real topic shift, not every sentence.
  Do not cluster every chapter in the first few minutes.
- tags: 10 to 15 search terms.

Return JSON with titles, description, chapters, and tags only.
"""


def _require_key(settings: Settings) -> None:
    if not settings.gemini_api_key:
        raise GeminiConfigError(
            "Gemini API key is not set. Open Settings and paste a Google AI Studio key."
        )


def _client(settings: Settings) -> genai.Client:
    _require_key(settings)
    return genai.Client(api_key=settings.gemini_api_key)


def _audio_mime(audio_path: Path) -> str:
    suffix = audio_path.suffix.lower()
    if suffix == ".mp3":
        return "audio/mpeg"
    if suffix in {".m4a", ".aac"}:
        return "audio/mp4"
    return "audio/wav"


def _file_state_name(uploaded: Any) -> str:
    state = getattr(uploaded, "state", None)
    if state is None:
        return ""
    name = getattr(state, "name", None)
    if isinstance(name, str) and name:
        return name.upper()
    text = str(state)
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text.upper()


def _wait_until_active(
    client: genai.Client,
    uploaded: Any,
    *,
    timeout_s: float = 180.0,
    sleep: Any = time.sleep,
) -> Any:
    """Poll Files API until ACTIVE. Returns immediately if already usable."""
    deadline = time.monotonic() + timeout_s
    current = uploaded
    interval = 0.25
    while True:
        state = _file_state_name(current)
        if state == "FAILED":
            raise GeminiConfigError("Gemini Files API processing failed for uploaded audio.")
        if state in {"", "ACTIVE"}:
            if getattr(current, "uri", None) or state == "ACTIVE":
                return current
        if time.monotonic() >= deadline:
            raise GeminiConfigError(
                "Timed out waiting for Gemini Files API upload to become ACTIVE."
            )
        sleep(interval)
        interval = min(interval * 1.5, 5.0)
        name = getattr(current, "name", None)
        if not name:
            uri = getattr(current, "uri", None)
            if uri:
                return current
            raise GeminiConfigError("Gemini Files API upload returned no file name or URI.")
        current = client.files.get(name=name)


def _part_from_uploaded(uploaded: Any, mime: str) -> types.Part:
    uri = getattr(uploaded, "uri", None)
    mime_type = getattr(uploaded, "mime_type", None) or mime
    if not uri:
        raise GeminiConfigError("Gemini Files API upload returned no file URI.")
    return types.Part.from_uri(file_uri=str(uri), mime_type=str(mime_type))


def _prepare_transcript_audio(audio_path: Path, settings: Settings) -> Path:
    """Prefer a compact 16 kHz mono MP3 when the WAV would exceed the inline limit."""
    if audio_path.stat().st_size <= INLINE_AUDIO_LIMIT_BYTES:
        return audio_path
    dest = audio_path.with_name(f"{audio_path.stem}_inline.mp3")
    try:
        extract_compact_audio(audio_path, dest, settings)
    except (MediaError, OSError) as exc:
        logger.warning("Could not compact audio for Gemini (%s); using Files API", exc)
        return audio_path
    if dest.is_file() and dest.stat().st_size > 0:
        logger.info(
            "Compacted transcript audio %s -> %s bytes",
            dest.name,
            dest.stat().st_size,
        )
        return dest
    return audio_path


def _audio_part(client: genai.Client, audio_path: Path) -> types.Part:
    """Build a real Part. Never put a raw Files API File into contents.parts."""
    size = audio_path.stat().st_size
    mime = _audio_mime(audio_path)
    if size <= INLINE_AUDIO_LIMIT_BYTES:
        logger.info("Uploading audio inline (%s bytes)", size)
        return types.Part.from_bytes(data=audio_path.read_bytes(), mime_type=mime)
    logger.info("Uploading audio via Files API (%s bytes)", size)
    uploaded = client.files.upload(file=str(audio_path))
    ready = _wait_until_active(client, uploaded)
    return _part_from_uploaded(ready, mime)


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
            child.layout = PictureTag.NOTHING
        fitted.append(child)
    return fitted


def stitch_director_plans(
    plans: list[DirectorPlan],
    windows: list[tuple[float, float]],
    duration: float,
) -> DirectorPlan:
    scenes: list[PlannedScene] = []
    for plan, (start, end) in zip(plans, windows):
        scenes.extend(fit_scenes_to_window(plan.scenes, start, end))
    scenes.sort(key=lambda item: item.start)
    if not scenes and duration > 0:
        scenes = [
            PlannedScene(
                start=0.0,
                end=duration,
                layout=PictureTag.NOTHING,
                reason="Fallback: director returned no scenes",
                said="",
                shown="full-frame host",
                asset_kind="none",
                graphic=GraphicCard(),
            )
        ]
    # Window metadata is discarded. plan_youtube_metadata owns titles/chapters.
    return DirectorPlan(scenes=scenes, metadata=YouTubeMetadata())


def sanitize_chapters(
    chapters: list[ChapterMarker],
    duration: float,
) -> list[ChapterMarker]:
    """Studio-legal chapter list used by normalize_youtube_metadata.

    First mark is 0. Gaps are at least 10 seconds. Pad to 3 when the cut
    is at least 30 seconds. This is the only chapter sanitizer.
    """
    duration = max(0.0, float(duration))
    cleaned = sorted(
        [
            ChapterMarker(start=max(0.0, float(chapter.start)), title=chapter.title.strip())
            for chapter in chapters
            if chapter.title.strip()
        ],
        key=lambda item: item.start,
    )
    if not cleaned or cleaned[0].start > 0.05:
        intro = cleaned[0].title if cleaned else "Intro"
        cleaned = [ChapterMarker(start=0.0, title=intro), *cleaned]
    cleaned[0] = ChapterMarker(start=0.0, title=cleaned[0].title)

    merged = [cleaned[0]]
    for chapter in cleaned[1:]:
        if chapter.start - merged[-1].start < 10:
            continue
        if duration > 0 and chapter.start >= duration:
            continue
        merged.append(chapter)

    if len(merged) < 3 and duration >= 30:
        labels = [chapter.title for chapter in merged]
        while len(labels) < 3:
            labels.append(f"Part {len(labels) + 1}")
        third = duration / 3.0
        merged = [
            ChapterMarker(start=0.0, title=labels[0]),
            ChapterMarker(start=max(10.0, third), title=labels[1]),
            ChapterMarker(start=max(20.0, min(duration - 0.01, third * 2)), title=labels[2]),
        ]
    return merged


def normalize_youtube_metadata(
    metadata: YouTubeMetadata,
    duration: float,
    *,
    fallback_title: str,
) -> YouTubeMetadata:
    """Force chapter, title, and tag rules the model often misses."""
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

    chapters = sanitize_chapters(metadata.chapters, duration)

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
    extras.extend(GENERIC_TAGS)
    for extra in extras:
        if len(unique_tags) >= 10:
            break
        if extra.casefold() not in seen_tags:
            unique_tags.append(extra)
            seen_tags.add(extra.casefold())
    unique_tags = unique_tags[:15]
    index = max(0, min(int(getattr(metadata, "title_index", 0) or 0), 4))
    return YouTubeMetadata(
        titles=unique,
        description=description,
        chapters=chapters,
        tags=unique_tags,
        title_index=index,
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
) -> str:
    lo, hi = expected_scene_range(end - start, settings)
    parts = [
        f"Full video duration: {duration:.2f} seconds.",
        f"Plan scenes for this window only: {start:.2f}s to {end:.2f}s.",
        "Use absolute timestamps on the full timeline.",
        f"Aim for about {lo} to {hi} scenes in this window. Cover the window with no gaps.",
        "Default layout is nothing. Overlay is the default markup for a spoken subpoint.",
        "PiP is rare and needs a named-platform still query. Never emit lower_third.",
        "Do not invent fonts, colors, zoom, or Scott.",
        "Omit metadata. Return empty titles, chapters, and tags.",
    ]
    if window_count > 1:
        parts.append(f"This is window {window_index + 1} of {window_count}.")
    parts.append(f"\nTimed transcript for this window:\n{transcript.window_text(start, end)}")
    return "\n".join(parts)


def _metadata_user_text(transcript: TimedTranscript, duration: float) -> str:
    parts = [
        f"Full trimmed duration: {duration:.2f} seconds.",
        "Write packaging for this entire cut. Chapters must span the whole duration.",
    ]
    if transcript.text:
        parts.append(f"\nFull transcript:\n{transcript.text}")
    if transcript.cues:
        parts.append(
            "\nTimed cues (trimmed timeline):\n"
            + "\n".join(
                f"[{cue.start:.2f}-{cue.end:.2f}] {cue.text.strip()}"
                for cue in transcript.cues
                if cue.text.strip()
            )
        )
    return "\n".join(parts)


def _transcript_upload_line(audio_path: Path) -> str:
    """Stdout line the desktop UI log can show before the transcript call."""
    size = audio_path.stat().st_size
    mode = "inline" if size <= INLINE_AUDIO_LIMIT_BYTES else "Files API"
    return f"Gemini audio: {audio_path.name} ({size} bytes), {mode}"


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
    audio_path = _prepare_transcript_audio(audio_path, settings)
    api = client or _client(settings)
    print(_transcript_upload_line(audio_path), flush=True)
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
                            )
                        ),
                    ],
                )
            ],
            schema=DirectorPlan,
            temperature=0.4,
        )
        plan = DirectorPlan.model_validate(payload)
        plan.metadata = YouTubeMetadata()
        plans.append(plan)

    stitched = stitch_director_plans(plans, windows, duration)
    metadata = plan_youtube_metadata(
        transcript,
        duration,
        settings,
        fallback_title=fallback_title,
        client=api,
    )
    script = EditScript(
        transcript=transcript.text,
        talking_head_cuts=[],
        scenes=[scene.to_scene() for scene in stitched.scenes],
        metadata=metadata,
    )
    return resolve_edit_script(script)


def plan_youtube_metadata(
    transcript: TimedTranscript,
    duration: float,
    settings: Settings,
    *,
    fallback_title: str,
    client: genai.Client | None = None,
) -> YouTubeMetadata:
    """Text-only packaging pass on the full transcript and full duration."""
    api = client or _client(settings)
    payload = _generate_json(
        api,
        model=settings.gemini_model,
        contents=[
            types.Content(
                role="user",
                parts=[
                    types.Part.from_text(text=_METADATA_PROMPT),
                    types.Part.from_text(text=_metadata_user_text(transcript, duration)),
                ],
            )
        ],
        schema=_PackagingSchema,
        temperature=0.4,
    )
    raw = YouTubeMetadata.model_validate(payload)
    return normalize_youtube_metadata(raw, duration, fallback_title=fallback_title)


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
        compact = settings.work_dir / f"{video_path.stem}_gemini.mp3"
        try:
            audio_path = extract_compact_audio(video_path, compact, settings)
        except MediaError:
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
        return resolve_edit_script(EditScript.model_validate_json(cleaned))
    except ValidationError:
        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise GeminiConfigError(f"Gemini JSON was not valid: {exc}") from exc
        try:
            return resolve_edit_script(EditScript.model_validate(payload))
        except ValidationError as exc:
            raise GeminiConfigError(f"Gemini JSON failed schema validation: {exc}") from exc


def load_edit_script(path: Path) -> EditScript:
    return parse_edit_script(path.read_text(encoding="utf-8"))
