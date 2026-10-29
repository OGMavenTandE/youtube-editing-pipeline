# Edit plan

The edit plan is the cut list. Booth can hand-edit it or import Episode 1's existing files. `ytp render` and `ytp check` both read it. The mapping is the same in YAML and JSON. The schema is [`edit-plan.schema.json`](edit-plan.schema.json).

## Import Episode 1

```bash
python -m ytp plan import \
  --timeline fixtures/ep01/timeline-v3d.json \
  --concat fixtures/ep01/rc8b-concat.txt \
  --score fixtures/ep01/score-cues-v3d.md \
  --lines fixtures/ep01/lines.json \
  --episode ep01 \
  --out path/to/ep01/edit/plan.yaml
python -m ytp plan validate path/to/ep01/edit/plan.yaml
```

What each source becomes:

| Source | Plan fields |
|---|---|
| `timeline-v3d.json` `clips` | `narration` and `captions` (one cue per line) |
| `timeline-v3d.json` `file`, `lead_in` | `voice`, `lead_in`. `voice` prefers `audio/mix/` when that folder has audio |
| `beat_titles`, `beat_starts`, `palette_segments` | `beats`, `palettes` |
| `shorts` | `shorts` (1080x1920) |
| `rc8b-concat.txt` | `segments` in order, still vs clip from the suffix |
| `score-cues-v3d.md` table | `music` (`at`, `sfx`, `gain_db`, `note`) |
| `lines.json` | not placed on the timeline. Ids missing from the timeline are listed in `meta.script_ids_not_in_timeline`. Episode 1's `00d_b1` was removed and must not come back. |

The concat demuxer repeats the last image with no duration. That repeat is not a second segment.

Score times are `M:SS.ss` (`1:12.01` is 72.01 seconds). Cue `source` is left empty because the sheet names the sound and does not path it. Point `source` at a file under `audio/score/` before render. `duck_under_voice` defaults to true.

Paths in the concat list are stored as written. On a real episode they should be relative to the episode directory (`frames/ep01-v3-000-coldopen.png`) so the plan moves with the folder.

## Fields the renderer uses

- `segments`: picture track. `kind: still` holds `duration` seconds. `kind: clip` plays `source` from `in` to `out`. `start` is the timeline time. A `template` name asks the graphics hook to draw the still.
- `broll`: overlays. `layout` is `full` or `pip-lower-right`. `at` and `duration` are timeline seconds.
- `music`: cues mixed under the voice. `gain_db` is applied to the file, then sidechain ducking when `duck_under_voice` is true.
- `voice`: the narration or the current mix. Import points this at `audio/mix/` when that folder has audio (`voice-v3d.wav` follows `mix-v3d.wav`, and the wav wins over an mp3 with that stem). `--voice voice` keeps the dry track. `--voice PATH` uses that file. Do not point it at an old render.
- `captions`: burned in, written as SRT, or both (`captions_mode`).
- `narration`: the approved words. The splice check compares the render to this text.
- `cards`: on-screen text. Each card has `text`, `start`, `duration`, and `style` (`title`, `lower-third`, or `end-card`). Talking-head renders place them outside the end-screen slots and append the end card for 7 seconds after the last spoken line.
- `audio_hook` / `graphics_hook`: optional scripts. Leave them null to use ffmpeg only.

The renderer places `voice` at timeline zero. It does not add `lead_in` a second time; the Episode 1 voice file already contains that second of silence.

Track's mix script, when set, is called unchanged:

```text
<script> --voice VOICE.wav --cues CUES.json --out MIX.wav --plan PLAN.yaml
```

Studio's graphics script is called once per segment that sets `template`:

```text
<script> --template NAME --out STILL.png --width W --height H --palette PALETTE --text TEXT --id SEGMENT_ID
```

`ytp/stub_hooks/track_mix.py` and `ytp/stub_hooks/studio_graphics.py` are argv stubs for tests. They are not Track's mix and not Studio's templates.

`version` is `1`. `ytp plan validate` checks version, durations, caption order, b-roll layout, and duplicate narration ids.
