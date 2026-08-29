# youtube-editing-pipeline

Local pipeline for landscape talking-head webcam footage: trim dead air, let Gemini pick scene layouts and YouTube copy, generate presentation slides, composite you full-screen or over those slides, then export an MP4 plus a YouTube Studio package.

```bash
python run.py --input raw_video.mp4
```

Input is always a landscape webcam recording of you. B-roll today means generated 1920x1080 slides. The B-roll provider interface is built so a later video-clip provider can slot in without changing the timeline.

## Setup

Python 3.10+ and FFmpeg on PATH are required.

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
```

Put your Google AI Studio key in `.env`:

```
GEMINI_API_KEY=your_key_here
```

Optional env vars: `GEMINI_MODEL` (default `gemini-2.5-flash`), `FFMPEG_PATH`, `OUTPUT_WIDTH` / `OUTPUT_HEIGHT` (default 1920x1080), `PIP_SCALE` (default `0.25`), `SILENCE_MIN_DURATION` (default `0.7`), `SILENCE_PADDING` (default `0.15`), `DIRECTOR_CHUNK_THRESHOLD` (default `480`), `DIRECTOR_CHUNK_SECONDS` (default `300`).

## FFmpeg

The CLI checks PATH (or `FFMPEG_PATH`) at startup and exits with install instructions if FFmpeg is missing.

macOS:

```bash
brew install ffmpeg
```

Linux (Debian/Ubuntu):

```bash
sudo apt-get update && sudo apt-get install -y ffmpeg
```

Windows:

```bash
winget install Gyan.FFmpeg
```

Chocolatey alternative: `choco install ffmpeg`. Confirm with `ffmpeg -version`.

pydub and auto-editor also need FFmpeg. `ffprobe` is expected next to `ffmpeg`.

## CLI

```bash
python run.py --input raw_video.mp4
python run.py --input raw_video.mp4 --output output/final.mp4
python run.py --input raw_video.mp4 --skip-silence
python run.py --input raw_video.mp4 --skip-gemini
python run.py --input raw_video.mp4 --auto-editor
python run.py --input raw_video.mp4 --edit-script output/raw_video_edit_script.json --skip-gemini
python run.py --input raw_video.mp4 --transcript output/raw_video_transcript.json
```

`--input` is required. Relative names are also resolved under `input/`. `--skip-silence` and `--skip-gemini` are for testing individual stages. `--transcript` reuses a saved JSON or plain-text transcript and skips the audio transcription pass.

## Layouts and pacing

A scene is a short beat, not the whole 20-minute file. The director plus a local pacing guard target **50-80 layout scenes** on a 20-minute trimmed cut.

Heavy change (one per scene):

- `FULL_FRAME`: you fill the 1920x1080 frame.
- `PIP_BOTTOM_RIGHT`: a slide fills the frame; you sit in a rounded lower-right bubble at about 25% width.
- `SPLIT_TOP`: you occupy the top two-thirds; a graphic occupies the bottom third.

Hold times: 8-15s in the first minute (target ~12s), then 15-25s (target ~20s). Same layout cannot run three times in a row. Order is content-driven, not a fixed A-B-C loop.

Light change (inside a hold, every ~5-7s): punch-in zoom (~1.15x), a short text takeaway, or a cut at the scene edge. These are not layout swaps.

If Gemini returns too few scenes, `pipeline/pacing.py` splits long holds and fills the timeline so the band still holds.

## Architecture

Swapable stages behind `run.py`. They share pydantic models, not implicit dicts.

1. `pipeline/silence_remover.py` — pydub energy detect + ffmpeg concat. Strip pauses longer than 0.7s, leave 0.15s pad on each side (~0.3s between sentences). Gaps under 0.7s stay. Writes a cut map. `--auto-editor` is an optional tighter pass.
2. `pipeline/gemini_director.py` — two Gemini 2.5 Flash passes (`google-genai`, `GEMINI_API_KEY`). First pass transcribes trimmed audio only (inline under 20MB, Files API above that) and writes `*_transcript.json`. Second pass is text-only: scenes (layout, reason, graphic card) plus YouTube metadata. Cuts longer than about 8 minutes are planned in 5-minute windows, then stitched. Micro-resets stay local in `pacing.py`. Talking-head filler cuts stay empty.
3. `pipeline/broll/` — slide provider now, video provider later. Same `BrollAsset` out.
4. `pipeline/compositor.py` — MoviePy 2 assembles the canvas from layout + webcam + slide.
5. Export — MP4 plus YouTube metadata. The UI (Task 7) will let you pick which of the five titles goes into the Studio text file.

`pipeline/config.py` loads dotenv and typed settings, including canvas size and PiP scale. `pipeline/layouts.py` is the layout enum.

## Notes

No API keys are stored in the repo. Intermediate media stays in `work/` and is gitignored with `output/`.
