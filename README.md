# youtube-editing-pipeline

Local CLI that turns a raw talking-head recording into a tighter YouTube cut: strip dead air, let Gemini 2.5 Flash write a structured edit script (cuts, lower-thirds, B-roll cues, takeaways, titles, description, chapters), then composite and render with MoviePy.

```bash
python run.py --input raw_video.mp4
```

## Setup

Python 3.10+ and FFmpeg on PATH are required.

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Put your Google AI Studio key in `.env`:

```
GEMINI_API_KEY=your_key_here
```

Optional env vars: `GEMINI_MODEL` (default `gemini-2.5-flash`), `FFMPEG_PATH`, `SILENCE_MIN_DURATION` (default `0.7`), `SILENCE_PADDING` (default `0.15`), `INPUT_DIR`, `OUTPUT_DIR`.

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
python run.py --input raw_video.mp4 --edit-script output/raw_video_edit_script.json --skip-gemini
python run.py --input raw_video.mp4 --transcript notes.txt --broll-dir assets/broll
```

`--input` is required. Relative names are also resolved under `input/`. `--skip-silence` and `--skip-gemini` are for testing individual stages. `--edit-script` replays a saved JSON plan.

Outputs land in `output/`:

- `<stem>_final.mp4`
- `<stem>_edit_script.json`
- `<stem>_youtube_metadata.json`
- `<stem>_cut_map.json` (when silence trim runs)

## Architecture

Three swapable stages behind `run.py`. They share pydantic models in `pipeline/models.py`, not implicit dicts.

1. `pipeline/silence_remover.py` — detect pauses longer than 0.7s, keep 0.15s of padding on each side, cut with auto-editor when installed or pydub + ffmpeg. Returns the trimmed path plus a cut map.
2. `pipeline/gemini_director.py` — upload trimmed audio to Gemini 2.5 Flash via the official `google-genai` SDK (`GEMINI_API_KEY`). Validate the JSON against `EditScript` (talking-head cuts, lower-thirds, B-roll/slide cues, overlay timestamps, five titles, SEO description, chapters).
3. `pipeline/compositor.py` — MoviePy 2 composites clean lower-third cards, takeaway callouts, and PiP/full-frame B-roll, then renders H.264/AAC. `ffmpeg-python` is used for probe/extract/concat helpers.

`pipeline/config.py` loads dotenv + typed settings. Each stage can be imported and run on its own.

The edit script is the source of truth after the director runs. Re-render with `--edit-script` without calling Gemini again.

## Edit script shape

```json
{
  "transcript": "...",
  "talking_head_cuts": [{"start": 0.0, "end": 42.0, "reason": "keep A-roll"}],
  "lower_thirds": [{"start": 1.2, "end": 5.0, "title": "Alex Chen", "subtitle": "Host"}],
  "broll": [{"start": 12.0, "end": 16.0, "query": "dashboard ui", "transition": "pip", "asset_path": null}],
  "overlays": [{"start": 20.0, "end": 24.0, "text": "Ship the cut, not the raw take", "kind": "takeaway"}],
  "metadata": {
    "titles": ["...", "...", "...", "...", "..."],
    "description": "...",
    "chapters": [{"start": 0.0, "title": "Intro"}],
    "tags": ["youtube", "editing"]
  }
}
```

B-roll files are optional. If `asset_path` is empty, the compositor looks in `--broll-dir` for a filename that matches `query`. Missing assets are skipped rather than failing the render.

## Notes

No API keys are stored in the repo. Intermediate media stays in `work/` and is gitignored with `output/`.
