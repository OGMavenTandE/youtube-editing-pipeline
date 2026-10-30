# Motion clips (`ytp motion`)

`kind: motion` frames in a scene spec are short animated clips (count-up bars,
kinetic type, chapter cards, a receipt) rendered with the HyperFrames CLI from
the templates in `templates/motion/`. Each one becomes a `kind: clip` segment in
the edit plan, so `ytp render` cuts it in like any other clip.

```
ytp motion edit/scenes.yaml                     # 16:9, writes clips/motion/motion-plan.yaml
ytp motion edit/scenes.yaml --aspect both       # plus the 9:16 Shorts layout
ytp motion edit/scenes.yaml --plan              # merge the 16:9 clips into edit/plan.yaml
ytp motion edit/scenes.yaml --only m-plan --lint-only
ytp scenes edit/scenes.yaml --plan              # stills AND motion, one step
```

| Flag | Meaning |
|---|---|
| `--episode DIR` | episode folder (default: the spec's `edit/` parent, like `ytp scenes`) |
| `--only ID` | one frame (repeatable) |
| `--aspect 16x9\|9x16\|both` | 9:16 is its own 1080x1920 layout with 1.4x type, never a crop |
| `--plan [PATH]` | merge the 16:9 segments into the plan (default `edit/plan.yaml`) |
| `--allow-placeholder` | render placeholder or sample copy anyway. Test renders only |
| `--force` | re-render even when cached |
| `--no-gpu` | skip NVENC, x264 only |
| `--lint-only` | plan, copy lint, build the projects and run `hyperframes lint`; no render |

Exit codes: 0 ok, 2 lint failure (copy, sizes, frame checks) or a missing tool.

`ytp scenes --plan` renders motion frames too (16:9). `ytp scenes --no-motion`
uses a motion clip only if it is already cached.

## What a motion frame looks like

```yaml
version: 1
series: AI Survival      # the label "AI SURVIVAL · EP 02" comes from these two
episode: 2
fps: 30
motion:                  # optional; every key also has an env var
  hyperframes: C:\AI Eval\YouTube\hyperframes
  fonts_dir: C:\AI Eval\YouTube\assets\fonts
frames:
  - id: ch1
    kind: motion
    template: chapter-card
    line: n12            # or start/duration, same as any frame
    layout: safe         # safe (default) | wide
    vars:
      title: "Step one: *Find* the waste"
```

* Untimed frames (`start` only) last as long as the clip. Timed frames longer
  than the clip get a `scene-<id>-hold` still of the last frame after the clip
  segment (`scene-<id>`). A slot shorter than the clip is an error: the hold
  would be cut.
* `clip_duration: 6` makes the clip itself longer (it can't be shorter than
  the animation plus its hold).
* `*word*` is the amber italic word. Headlines need exactly one (chapter cards
  only warn when there is none).

## Finding the tools

| What | Order |
|---|---|
| HyperFrames CLI | `motion.hyperframes` in the scenes file, then `YTP_HYPERFRAMES`, then a `hyperframes/node_modules/hyperframes/bin/hyperframes.mjs` in any parent folder of the spec, then `hyperframes` on PATH. A folder, `.mjs`, `.cmd` (run through its `.mjs`) or `.py` all work. `YTP_NODE` picks node |
| GSAP | `motion.gsap`, then `YTP_GSAP`, then `node_modules/gsap/dist/gsap.min.js` beside the CLI. Copied into each project; never a CDN |
| Fonts | `motion.fonts_dir`, then `YTP_MOTION_FONTS`, then `assets/fonts` in any parent folder. Not committed |

Fonts needed: `Fraunces-Variable.ttf`, `Fraunces-Italic-Variable.ttf`
(headlines), `IBMPlexSans-Variable.ttf`, `IBMPlexMono-Regular.ttf`,
`IBMPlexMono-SemiBold.ttf` (labels, sources, figures, receipt). Inter stays
for captions only. All are OFL; get them from the Fraunces and IBM Plex GitHub
releases.

Every HyperFrames call runs with `HYPERFRAMES_NO_TELEMETRY=1`, `DO_NOT_TRACK=1`
and `HYPERFRAMES_NO_UPDATE_CHECK=1` (plus the npm/Chrome update switches), and
the project `.env` carries the two telemetry switches too.

## Render, encode, cache

1. Plan: fit every element to the stage with the real fonts (Pillow), wrap and
   balance lines, compute cues and the duration (whole frames).
2. Lint the copy and sizes (below). Errors stop the frame.
3. Build `<episode>/.ytp-motion/<id>-<aspect>/`: `index.html`, fonts, gsap,
   `house.js`, the template, `ytp-plan.json`.
4. `hyperframes lint`, then `hyperframes render --quality high --fps N --gpu`.
   If the GPU render fails it retries without `--gpu`. The encoder actually used
   is read back from the file (HyperFrames falls back to x264 silently on a
   machine without NVENC) and logged.
5. Check the frames (below), then move the clip to
   `<episode>/clips/motion/<id>-<aspect>-<hash>.mp4` with a `.json` sidecar.

The hash covers the vars, layout, timing, template files, fonts, GSAP and the
HyperFrames version. A matching clip is reused; older clips of the same frame
and aspect are removed.

## Lint rules

Copy:

* Every var a template declares is required unless marked optional; templates
  have no default text.
* `source` is required wherever a figure appears (count-up, receipt, any text
  with digits outside a label).
* Placeholder or sample copy fails: `[...]`, `placeholder`, `TODO`, `TBD`,
  `lorem`, `xxx`, `{{...}}`, and the pilot slogans ("Short prompts. Fresh chats.
  Smaller bills.", "Three habits. No new tools."). `--allow-placeholder` turns
  these into warnings for test renders.
* AI vendor and model names fail. No em dashes.

Type, at 1080 high (9:16 is 1.4x):

| Role | Minimum |
|---|---|
| label | 26px, uppercase, letter-spaced, IBM Plex Mono |
| body, subline | 40px |
| source | 24px at 70% opacity or more, IBM Plex |
| headline | 56px, Fraunces, one amber italic word |

Layout: in 16:9 `layout: safe` (default) every type box stays inside x 656-1264,
the slice a 9:16 crop keeps. `layout: wide` uses 160-1760. Copy that can't fit
at the minimum size fails.

Timing and frames (checked on the render):

* Frame 0 is ink plus grain, never black; the first element is visible by
  frame 6.
* The last line holds max(1.5 s, 0.3 s per word), then hard-cuts. No fade.
* The first and last frames are compared with the ink (not black); the last
  frame must still show type and must not be darker than 1.4 s earlier.
* Size, frame count and duration must match the plan.
* 16:9 safe: nothing bright outside the slice.

## Templates

### `count-up`

Two bars. The before bar is muted slate with white text and counts up first;
the after row stays hidden until the before number lands, then the amber after
bar and number count up. Tracks are about 6% white. The tag lands last; the end
holds 1.5 s or more.

| Var | |
|---|---|
| `label` | top label |
| `title` | headline, one `*amber*` word |
| `before_label`, `after_label` | row labels |
| `before`, `after` | numbers |
| `unit` | e.g. `tokens` |
| `decimals` | optional, default 0 |
| `tag` | optional result line |
| `source` | required |

### `kinetic`

One line on screen at a time: each line exits in 7 frames (ease-in), 2 frames
of ink, the next enters (ease-out). The subline is the last line and holds.

| Var | |
|---|---|
| `label` | stays top-left |
| `lines` | list of lines |
| `amber_word` | one word in one line, set amber italic |
| `subline` | the closing line |
| `source` | optional (required if a line has figures) |

### `chapter-card`

Label (auto: `SERIES · EP NN` from the scenes file), an amber rule, the title
word by word, an optional subline.

| Var | |
|---|---|
| `title` | one `*amber*` word |
| `label` | optional override of the auto label |
| `subline`, `source` | optional |

### `receipt-unroll`

A paper receipt unrolls line by line, then the total in red. Lint warns if the
lines don't add up to the total.

| Var | |
|---|---|
| `label`, `title` | as above |
| `lines` | list of `{label, amount}` |
| `total_label`, `total`, `unit` | |
| `decimals` | optional |
| `source` | required |

## Ep02 example (Galley v1 copy)

From `ai-survival/ep02/motion-copy-galley-v1.md`. The receipt has no Galley
copy yet, so its vars are marked placeholders and need `--allow-placeholder`
until Galley writes them. Times are examples; use `line:` ids from the script.

```yaml
version: 1
series: AI Survival
episode: 2
fps: 30
frames:
  - id: m-countup                # section 7, "the trick"
    kind: motion
    template: count-up
    start: 0
    vars:
      label: "ONE TASK, BEFORE AND AFTER"
      title: "Tokens for one task, *before* and after"
      before_label: "BEFORE: THE MODEL READS ALL 10,000 ROWS"
      after_label: "AFTER: CODE FILTERS, THE MODEL READS 5"
      before: 150000
      after: 2000
      unit: tokens
      tag: "98.7% fewer tokens"
      source: "Example from a major AI lab, Nov 2025"
  - id: m-plan                   # section 3 preview, again at section 10
    kind: motion
    template: kinetic
    start: 8
    vars:
      label: "THE PLAN"
      lines:
        - "Find the waste."
        - "Trim what it reads."
        - "Move right-answer work into code."
        - "Right-size the model."
      amber_word: code
      subline: "Then test it before it replaces anything."
  - {id: m-ch0, kind: motion, template: chapter-card, start: 20, vars: {title: "How the bill works"}}
  - {id: m-ch1, kind: motion, template: chapter-card, start: 25, vars: {title: "Step one: *Find* the waste"}}
  - {id: m-ch2, kind: motion, template: chapter-card, start: 30, vars: {title: "Step two: *Trim* what it reads"}}
  - {id: m-ch3, kind: motion, template: chapter-card, start: 35, vars: {title: "Step three: Give right-answer work to *code*"}}
  - {id: m-ch4, kind: motion, template: chapter-card, start: 40, vars: {title: "The trick: Have the AI *write* the code"}}
  - {id: m-ch5, kind: motion, template: chapter-card, start: 45, vars: {title: "Step four: *Right-size* the model"}}
  - {id: m-ch6, kind: motion, template: chapter-card, start: 50, vars: {title: "*Test* it before it replaces anything"}}
  - {id: m-ch7, kind: motion, template: chapter-card, start: 55, vars: {title: "Where to start *this week*"}}
  - id: m-receipt                # NO GALLEY COPY YET: placeholders
    kind: motion
    template: receipt-unroll
    start: 60
    vars:
      label: "[PLACEHOLDER] THE BILL"
      title: "[PH] One order, *every* page"
      lines:
        - {label: "[PH] REREAD 40-PAGE BINDER", amount: 96000}
        - {label: "[PH] REREAD CHAT HISTORY", amount: 38000}
        - {label: "[PH] FULL TOOL LIST", amount: 14000}
        - {label: "[PH] THE ACTUAL ORDER", amount: 2000}
      total_label: "[PH] TOTAL"
      total: 150000
      unit: tokens
      source: "[PLACEHOLDER] source line"
```

Notes on the Galley copy:

* Galley marks no amber word for the kinetic lines; `amber_word: code` is a
  pick, change it if Studio wants another.
* "How the bill works" has no amber word (a warning, not an error).
* The count-up headline keeps Galley's amber *before*; the bars follow the
  rule (slate before, amber only for the after bar and number).

## Tests

`tests/ytp/test_motion.py` runs against `tests/ytp/fixtures/mock_hyperframes.py`
(a fake CLI that draws ink plus a box with ffmpeg) and needs Pillow and ffmpeg;
it skips without Pillow.
