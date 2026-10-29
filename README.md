# youtube-editing-pipeline

## Booth quickstart (`ytp`)

`ytp` is the script-driven editor for series episodes (AI Survival first). It never publishes a video and never changes a YouTube listing. Python 3 and ffmpeg/ffprobe only. No CapCut, DaVinci, or paid APIs. The same commands run on Linux and on Windows, including WSL on Scott's PC. NVIDIA encode is optional later (`--encoder nvenc` on the RTX 4070 Super); the default encoder is libx264 so a machine without a GPU still renders.

```bash
pip install -e .
# optional local transcription (pins PyAV below 17; 16.1.0 works):
pip install -e ".[whisper]"
# ytp is on PATH after that install, from any working directory:
ytp init path/to/ep01 --series ai-survival
python -m ytp rule list --series ai-survival
python -m ytp rule add --series ai-survival --section edit --text "Never say \"widget\"."
python -m ytp plan import \
  --timeline path/to/timeline-v3d.json \
  --concat path/to/rc8b-concat.txt \
  --score path/to/score-cues-v3d.md \
  --lines path/to/lines.json \
  --out path/to/ep01/edit/plan.yaml
python -m ytp plan validate path/to/ep01/edit/plan.yaml
python -m ytp index path/to/ep01
python -m ytp render path/to/ep01/edit/plan.yaml --captions both
python -m ytp check path/to/ep01/renders/v1/plan.mp4 \
  --plan path/to/ep01/edit/plan.yaml \
  --series ai-survival
```

`init` creates `source/vo`, `source/raw`, `audio/voice`, `audio/mix`, `audio/score`, `frames`, `edit`, `renders`, `shorts`, `thumbs`, `text`, and `qc`. How a note from Scott becomes a rule is written up in [docs/rules.md](docs/rules.md). The cut list is documented in [docs/edit-plan.md](docs/edit-plan.md).

`index` transcribes every clip under `source/` with local faster-whisper (`pip install -r requirements-ytp-whisper.txt`) and writes `edit/clip-index.json` plus `edit/clip-index.md` (path, duration, word timestamps, transcript, description). A description you already filled in is kept. `--backend none` records durations only.

`render` concats stills and clips, overlays B-roll (`full` or `pip-lower-right`), ducks music under the voice, and writes captions as a sidecar SRT, a burn-in, or both. `--encoder nvenc` (or `auto`) uses the RTX 4070 Super when that encode actually succeeds; otherwise the render stays on libx264. `--audio-hook` calls Track's mix script unchanged. `--graphics-hook` calls a Studio template script for any segment that sets `template`. The files in `ytp/stub_hooks/` are argv stubs for tests, not those tools.

`render` with no `--out` writes `renders/v1/`, then `renders/v2/`, and so on. It will not overwrite a file that is already there. `--out` is for an explicit new path only.

`check` writes `qc/check-report.md` and `qc/check-report.json`. It fails the render for caption drift, loudness (voice -16 LUFS, Shorts -14, true peak at or below -1.5 dBTP, music about 17 dB under the voice when stems are on disk), banned words and vendor names, closer lines, banned sound-effect ids, and leftover words at splices. Pace is a warning, not a failure: overall words per minute (pauses included; AI Survival warns outside 155–185) and articulation words per minute (only a gap between narration lines longer than 250 ms is removed; a hole inside a line is not a pause; warns outside 175–185). Desk takes only warn. The splice check aligns one transcript of the checked audio to the plan text and flags only unmatched words near a narration join. It does not read the plan's voice file and it does not transcribe each cut again. Episode 1 v8 left "It's" (removed line 00d_b1) just before 18s; that is the fragment. The process exits 1 on a failure. Pass `--asr` with word-timestamp JSON when you already have it. A bracket transcript (`[start-end] text`) has no word times: the splice check and articulation pace are reported as not measured instead of guessing. Otherwise install faster-whisper so the check can listen itself. Whisper is locked to the series language, English by default. On-camera videos use `--series talking-head`.

Run the suite (Whisper tests stay out of CI):

```bash
python -m pytest tests/ytp -m "not whisper" --confcutdir=tests/ytp
```

### Full pass on an existing episode folder

Put the voice, mix, and score under `audio/`, stills under `frames/`, and raw clips under `source/`. `init` only adds folders; it does not move or delete media. From the repo:

```bash
python -m ytp init /path/to/ep01 --series ai-survival
python -m ytp index /path/to/ep01
python -m ytp plan import \
  --timeline /path/to/ep01/timeline-v3d.json \
  --concat /path/to/ep01/rc8b-concat.txt \
  --score /path/to/ep01/score-cues-v3d.md \
  --lines /path/to/ep01/lines.json \
  --out /path/to/ep01/edit/plan.yaml
python -m ytp plan validate /path/to/ep01/edit/plan.yaml
python -m ytp render /path/to/ep01/edit/plan.yaml \
  --captions both \
  --encoder auto
python -m ytp check /path/to/ep01/renders/v1/plan.mp4 \
  --plan /path/to/ep01/edit/plan.yaml \
  --series ai-survival
```

To check a cut you already have, skip render and pass the mix transcript if Whisper is not installed:

```bash
python -m ytp check /path/to/ep01/renders/rough-cut-v8b.mp4 \
  --plan /path/to/ep01/edit/plan.yaml \
  --series ai-survival \
  --asr /path/to/ep01/asr-mix-v3d.txt
```

Point `voice` in the plan at the current mix before rendering. Do not point it at an older render. Nothing here publishes or edits the YouTube listing.

## Webcam desk pipeline

Local pipeline for landscape talking-head webcam footage: trim dead air, let Gemini tag sparse body beats, paint a locked Scott Mastin picture kit (overlay / pip / nothing, plus app-forced bookends), then export an MP4 plus a YouTube Studio package.

```bash
python run.py --input raw_video.mp4
```

Input is always a landscape webcam recording of Scott. Chrome is the locked kit in `pipeline/picture_kit.md`. PiP stills are DVIDS 16:9 named-platform files from `--broll-dir`. No stock-footage APIs. Chromium slides are not the look.

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

Optional env vars: `GEMINI_MODEL` (default `gemini-3.6-flash`), `FFMPEG_PATH`, `OUTPUT_WIDTH` / `OUTPUT_HEIGHT` (default 1920x1080), `PIP_SCALE` (default `0.25`), `SILENCE_MIN_DURATION` (default `1.0`), `SILENCE_PADDING` (default `0.30`), `SILENCE_THRESHOLD_DB` (default `-45`), `DIRECTOR_CHUNK_THRESHOLD` (default `480`), `DIRECTOR_CHUNK_SECONDS` (default `300`), `TARGET_LUFS` (default `-14`).

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
python run.py --input raw_video.mp4 --transcript output/raw_video_transcript.json --skip-composite
python run.py --input raw_video.mp4 --edit-script output/raw_video_edit_script.json --skip-gemini
python run.py --input raw_video.mp4 --edit-script output/raw_video_edit_script.json --skip-composite
python run.py --input raw_video.mp4 --broll-dir broll/
python run.py --input raw_video.mp4 --skip-slides
python run.py --input raw_video.mp4 --skip-studio
python run.py --input raw_video.mp4 --title-index 2
python run.py --repack-studio output/raw_video_studio --title-index 2
```

`--input` is required for a full run. Relative names are also resolved under `input/`. `--skip-silence`, `--skip-gemini`, `--skip-slides`, `--skip-studio`, and `--skip-composite` are for testing individual stages.

`--transcript` reuses a saved JSON or plain-text transcript and skips the audio transcription pass. A later `--skip-composite` run with that transcript re-plans scenes and writes the edit script, then stops before MoviePy. The plan path is printed as `Plan: output/<stem>_edit_script.json`.

`--edit-script` composites the trimmed talking-head when `work/<stem>_trimmed.mp4` exists. `--skip-silence` only passthroughs the raw `--input` when there is no trimmed file and no cut map.

`--broll-dir` matches local video filenames against scene graphic titles and `BrollCue.query`. A hit becomes the PIP/SPLIT/FULL_FRAME graphic layer. No match keeps the generated slide.

`--title-index` (0-4) picks which of the five titles is the paste title and the thumbnail line. If omitted, the pipeline reuses `title_index` from `*_youtube_metadata.json` so a desk pick survives a full rerun.

`--auto-editor` is an optional tighter silence pass. The cut map written afterward describes the file that was actually rendered (probed duration), not the pydub preview map.

After the MP4 lands, the pipeline writes `output/<stem>_studio/`: a copy or hardlink of the video, `titles.txt` (chosen title on line 1, the other four below, plus `selected: N`), `description.txt` (SEO body plus one YouTube-legal chapter list), `tags.txt`, `captions.srt` / `captions.vtt` when a transcript exists, and thumbnail candidates. `thumbnail.jpg` is the 25% webcam card. `thumbnail_01.jpg` / `thumbnail_02.jpg` / `thumbnail_03.jpg` are the 10% / 25% / 50% frames. Drag that folder into YouTube Studio. There is no YouTube API upload. `*_youtube_metadata.json` stays the machine file and stores `title_index`.

`--repack-studio` rewrites an existing Studio folder without silence trim, Gemini, slides, or MoviePy. Pass the `output/<stem>_studio` folder, the stem, or the original input that already has a studio folder plus `output/<stem>_youtube_metadata.json` and the final MP4. Captions and extra thumbs are rewritten when the transcript and webcam file are present. The thumbnail still needs the trimmed talking-head file (`work/<stem>_trimmed.mp4`) or the original webcam via `--input`. If that frame source is missing, the command fails instead of drawing a black frame. The existing MP4 is hardlinked or copied as before.

## Studio review UI

Task 7 is desk review, not the editor. It does not run the pipeline, edit scenes, or upload to YouTube.

```bash
streamlit run ui.py
```

`python -m pipeline.ui` also works. Open the local URL, pick an `output/*_studio` folder, choose one of the five titles, read or edit the description and chapters, pick a thumbnail candidate, and rewrite the folder. Same writer as the CLI: `write_studio_package()`. Localhost only. No auth and no deploy.

## Windows app

Always-on desktop watcher for the same local pipeline. No terminal. No YouTube upload. Phone drop goes to a Google Drive inbox; the PC downloads the MP4, runs `run.py`, and uploads `output/<stem>_studio/` to Drive `outbox/<stem>/`.

### Download

GitHub Actions builds `youtube-pipeline.exe` (one-folder) on every pull request and on tags. Open the workflow run → Artifacts → `youtube-pipeline-windows`. Unzip next to a writable folder. Chromium is bundled in the zip and copied to `%APPDATA%\YouTubePipeline\ms-playwright` so a later unzip does not wipe it. FFmpeg stays on the PC.

From a source checkout:

```bash
pip install -r requirements.txt
python desktop/app.py
```

### First-run wizard

Double-click `youtube-pipeline.exe` (or run `python desktop/app.py`). The first launch walks through one question per screen:

1. What the app does.
2. FFmpeg on PATH. If it is missing, the window shows copyable `winget install Gyan.FFmpeg` and `choco install ffmpeg`, plus Recheck. Install Chromium uses the bundled Playwright driver (no system Python) and writes to AppData.
3. Gemini key. Saved as `GEMINI_API_KEY` in the user data dir (`%APPDATA%\YouTubePipeline\.env` on Windows), not next to the EXE. Unzipping a new build into a new folder keeps the key. Never logged.
4. Google Drive sign-in. Desktop OAuth client, Drive scope only. Paste `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET`, or point at `desktop/client_secret.json`. A browser popup completes login. The refresh token is stored with Windows DPAPI (or a user-only file).
5. Folders. Create or pick `YouTube Pipeline/inbox`, `YouTube Pipeline/outbox`, and `YouTube Pipeline/done`. Folder IDs are saved locally.
6. Start with Windows. A Startup-folder launcher. Finish.

### After setup

The main window plus a tray icon stay running. Status is Idle / Watching / Downloading / Talk sheet / Processing / Uploading / Error. Close hides to the tray. Quit is in the tray menu (and on the window). Pause watching or Run once now from either place.

The main window is the talk-sheet form: open title + two-line overview, Point 1–3 platform query / optional "Starts when I say" cue / one PiP still / optional image title (gold kicker) plus image text (white content) on that still / two or three overlay cards each with a title (gold kicker) and headline, and a locked close + identity preview. Point picture locks to the trimmed transcript (cue or first card-line hit), not a clock time. Paste, load, or copy `Documents\\Youtube Pipeline\\talk_sheet.md`. Save writes JSON next to the video stem and a last-used copy in `%APPDATA%\\YouTubePipeline\\`. Empty fields stay empty until Run; the kit/director fills those only. User text and a browsed still are locked. Image text is never split on a period to invent a still title. Auto kickers are a short label of the point, never a guessed statute, and never say DoD.

When a Drive inbox video is ready, encode pauses until Run / Continue job. Empty form + Run is a full auto-fill job. The window comes to the front. The pipeline does not start behind the form, and a prefilled last-used sheet does not skip the wait. Browsed stills are copied into `Documents\\Youtube Pipeline\\stills` (or the work stills dir) as `pointN_<platform>.jpg` and passed as `--broll-dir`.

Record on the phone, drop a landscape MP4 in the Drive inbox, fill or skip the form, then open the outbox folder from the app. After a successful run you can pick titles 1–5. That calls the existing `--repack-studio --title-index` path and re-uploads the Studio folder. It is one screen, not the Streamlit reviewer.

`--broll-dir` is the job stills folder. Settings can reopen the Gemini key, Drive sign-in, folders, and startup screens.

The CLI is unchanged: `python run.py --input …` still works.

## Picture kit

Locked look. Spec: `pipeline/picture_kit.md`. One font (Inter, bundled). Two colors: white `#FFFFFF` and gold `#E0B44A`. Dark plate rgb(8,10,14) at ~200 alpha. Host is always the real camera.

The model may only tag a **body** beat `overlay` | `pip` | `nothing` and fill template copy. It does not choose layout, font, colors, zoom, or generate Scott. Sparse: overlay is the default markup; PiP is rare; most beats are nothing.

The app forces bookends on the first and last ~10s (`BOOKEND_SECONDS`):

- Open: `open_card` (title kicker + two-line thesis from the talk sheet) plus the two-column identity / FIND ME lower third. Not a body tag. Not Point 1.
- Close: locked `close_card` (`WORK WITH ME` / Independent AI T&E. Vendor-agnostic.) plus the same identity bar. Never a PiP. No WRAP kicker.

Identity strings are config (`HOST_NAME`, `HOST_TITLE_LINE`, `HOST_AFFILIATIONS`, `HOST_MISSION`, `HOST_FIND_ME`). Talk-sheet copy is job metadata (`TALK_TITLE`, `TALK_EXEC_HEADLINE`, or `--talk-sheet` / `<stem>_talk_sheet.json`). User-filled fields carry a `user` source flag so Gemini and DVIDS cannot clobber them.

Tagged beats are written to `output/<stem>_tagged_beats.json` before encode so a retry skips the model.

If `talking_head_cuts` is non-empty on the edit script, those keep-ranges are applied on the trimmed timeline before composite and scene timestamps are remapped. An empty list is ignored.

## Composite and loudness

Each scene encodes to `work/scenes/<stem>/`. If that file already exists and its fingerprint matches, the encode is skipped (resume). ffmpeg concatenates the scene files and applies a single-pass `loudnorm` toward YouTube's about -14 LUFS (`TARGET_LUFS`). Final mux is H.264/AAC. If ffmpeg concat fails, the compositor falls back to an in-memory MoviePy concat and says so.

## Architecture

Swapable stages behind `run.py`. They share pydantic models, not implicit dicts.

1. `pipeline/silence_remover.py` — pydub energy detect + ffmpeg concat. Strip pauses longer than 1.0s, leave 0.30s pad on each side (~0.6s between sentences). Gaps under 1.0s stay. Energy is measured on a 100ms window so a breath before a word does not eat the onset. Isolated uh/um under 0.35s still drop. Writes a cut map that matches the file handed to the director. `--auto-editor` is an optional tighter pass; its cut map uses the rendered duration.
2. `pipeline/gemini_director.py` — two Gemini 3.6 Flash passes (`google-genai`, `GEMINI_API_KEY`). First pass transcribes trimmed audio only (inline under 20MB, Files API above that) and writes `*_transcript.json`. Body tags are text-only (`overlay` | `pip` | `nothing`) and do not write YouTube copy. After windows are stitched, a dedicated text-only metadata pass runs on the full transcript and full duration (titles, description, chapters for the whole cut). Bookends are applied in `pacing.py`.
3. `pipeline/picture_kit.py` — PIL renderer for overlay, PiP type, and bookend chrome. Inter from `pipeline/fonts/`.
4. `pipeline/stills.py` — DVIDS 16:9 still matcher for PiP. Banana is a stub that may fill the image slot only.
5. `pipeline/compositor.py` — ffmpeg `filter_complex` first (MoviePy fallback) on a 1920x1080 canvas. `nothing` is full-frame host. `overlay` and bookends overlay kit PNGs. `pip` scales the entire talking-head frame into a 560x315 window (not a face crop) over a still. No punch-in zoom. Hard cuts only.
6. `pipeline/studio.py` — after the MP4, write `output/<stem>_studio/`. Copy or hardlink the video (no second encode). Reuses `normalize_youtube_metadata()` for titles, chapters, and tags. Paste files: `titles.txt`, `description.txt`, `tags.txt`, `captions.srt` / `captions.vtt`, and thumbnail candidates. JSON metadata stays the pipeline source of truth, including `title_index`. No YouTube Data API upload.
7. `pipeline/ui.py` — Streamlit review page. Pick a finished Studio folder, a title, description, chapters, and a thumbnail candidate, then call `write_studio_package()`. `python run.py --repack-studio` is the same rewrite without opening the UI. Not a scene editor and not a pipeline runner.
8. `pipeline/drive_io.py` plus `desktop/` — Windows CustomTkinter tray app. Drive API resumable download/upload, claim-by-file-id, move inbox → done, processed-id store. Calls `run.py` / `repack_studio` on a worker thread. No YouTube Data API.

`pipeline/config.py` loads dotenv and typed settings, including canvas size, PiP scale, and `target_lufs`. `pipeline/layouts.py` is the layout enum. Frozen builds treat the folder next to the exe as the install root. Credentials stay in the user data dir so replacing the EXE zip is safe.

## Notes

No API keys are stored in the repo. The Windows app keeps the Gemini key and OAuth client values in the user data dir (`%APPDATA%\YouTubePipeline` on Windows). Intermediate media stays in `work/` and is gitignored with `output/`.
