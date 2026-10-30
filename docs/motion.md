# Motion clips (`ytp motion`)

`kind: motion` frames in a scene spec are short animated clips (count-up bars,
kinetic type, chapter cards, a receipt, a receipt image) rendered with the HyperFrames CLI from
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

Every clip, both aspects, gets a `.last.png` hold still beside it.

The composition length is written a hair under the last frame's end
(`render_duration`), because HyperFrames rounds the frame count up.

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
* No prices or currency, ever: `$ € £ ¥ ¢ ₹ ₩ ₽`, `USD`, `EUR`, `GBP`, `dollars`,
  `cents`, `bucks`, `euros` fail in any var. No template, default or formatter
  adds one (a test scans the template files and every planned payload).

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

* Frame 1 is never empty: the top label (and the amber rule on chapter cards)
  is on screen, full opacity, from the first frame. Everything else animates as
  before. The render check fails a clip whose first frame has under 40 lit pixels
  (brighter than ink by 40) in the type area.
* Frame 0 is ink plus grain, never black.
* A number is hidden until its count is already past the start value: the planner
  sets a `*_show` cue a quarter frame before the first frame whose value is non-zero,
  so "0 tokens" never shows, not even for one frame (the tests walk every frame).
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
of ink, the next enters (ease-out). The subline is the last line and holds; it is
set in Fraunces 60-72px (84-100 at 9:16), as strong as the lines before it.

| Var | |
|---|---|
| `label` | stays top-left |
| `lines` | list of lines |
| `amber_word` | one word in one line, set amber italic |
| `subline` | the closing line |
| `source` | optional (required if a line has figures) |

### `chapter-card`

Label (auto: `SERIES · EP NN` from the scenes file), an amber rule, the title
word by word, an optional subline. All chapter cards in one scenes file share
one title size per aspect (the smallest any card needs), worked out over the
whole file so `--only` renders match.

| Var | |
|---|---|
| `title` | one `*amber*` word |
| `label` | optional override of the auto label |
| `subline`, `source` | optional |

### `receipt-unroll`

A paper receipt hangs from a fixed header (or a short stub with no header) and
grows down one line at a time, then the total lands in red. It never scrolls: the
plan sizes everything so every line is on screen at the total (text 40px or more,
56px at 9:16) and fails if the copy can't fit. Six lines fit in both aspects.
`TOTAL:` and the figure sit on one row when they fit at 40px, else stack. Lint
warns if amounts don't add up to the total.

| Var | |
|---|---|
| `header` | optional, printed at the top of the paper |
| `label`, `title` | optional, above the paper |
| `lines` | list of text lines, or `{label, amount}` |
| `total_label`, `total`, `unit` | e.g. `TOTAL:`, `150000`, `TOKENS` |
| `decimals` | optional |
| `source` | required |

### `receipt-image`

Studio's finished receipt art (a PNG strip) prints in from a fixed top edge, then
holds; hard cut. The image is the art: it is never redrawn, recolored or cropped,
only revealed. A dark slot sits at the top edge, and on frame 1 the slot plus a
28px lip of blank paper (above the first ink) is already on screen. The paper
then feeds down at a steady speed for `print` seconds over the house ink and
grain, and holds for `hold` seconds.

* 16:9: drawn 1:1, centred on the Shorts slice (a 608px strip fills x 656-1264),
  hanging from y 72 so the frame-1 lip is inside the stage (Studio's spec said 60;
  the tallest strip, 918px, ends on the stage bottom at 990). Never enlarged; a
  strip wider than 608px or taller than 918px is shrunk to fit, with a warning.
* 9:16: one scale that fits the safe area (x 90-990, y 200-1720). A 608px strip
  is scaled to 900px (1.48x), so 40px receipt type lands at about 59px. The art
  is upscaled by the browser; for the sharpest 9:16, give Studio's 2x strip as
  `image_9x16` so it is scaled down instead.
* The path and timings never reach the screen, so the copy lint (currency,
  vendors, placeholders) has nothing to check: Studio's art carries its own copy.
* A new image (by content) changes the hash and renders again.

| Var | |
|---|---|
| `image` | PNG, absolute or relative to the episode folder |
| `image_9x16` | optional PNG for 9:16 only, e.g. Studio's 2x strip (1216px), scaled down (0.74x) to the safe area instead of the 1x strip scaled up; default `image` |
| `print` | optional, seconds to print in (default 3.0, 0.5 to 10) |
| `hold` | optional, seconds held after the print (default 2.0) |

Ep02 v4 (Studio's six strips in the episode's `art-v4\receipts\strips\`, and the
2x set in `strips-2x\` for 9:16):

```yaml
frames:
  - {id: m-r0-open,  kind: motion, template: receipt-image, start: 0,  vars: {image: art-v4/receipts/strips/R0-open.png, image_9x16: art-v4/receipts/strips-2x/R0-open.png}}
  - {id: m-r1-step1, kind: motion, template: receipt-image, start: 8,  vars: {image: art-v4/receipts/strips/R1-step1.png, image_9x16: art-v4/receipts/strips-2x/R1-step1.png}}
  - {id: m-r2-step2, kind: motion, template: receipt-image, start: 16, vars: {image: art-v4/receipts/strips/R2-step2.png, image_9x16: art-v4/receipts/strips-2x/R2-step2.png}}
  - {id: m-r3-step3, kind: motion, template: receipt-image, start: 24, vars: {image: art-v4/receipts/strips/R3-step3.png, image_9x16: art-v4/receipts/strips-2x/R3-step3.png}}
  - {id: m-r4-step4, kind: motion, template: receipt-image, start: 32, vars: {image: art-v4/receipts/strips/R4-step4.png, image_9x16: art-v4/receipts/strips-2x/R4-step4.png, hold: 1.0}}
  - {id: m-r5-final, kind: motion, template: receipt-image, start: 40, vars: {image: art-v4/receipts/strips/R5-final.png, image_9x16: art-v4/receipts/strips-2x/R5-final.png}}
```

Studio's spec holds R4 for 1 s and R5 for 2 s; set `start` from the script lines.

## Ep02 example (Galley v2 copy)

From `ai-survival/ep02/motion-copy-galley-v2.md`. Strict lint passes (no
placeholders). Times are examples; use `line:` ids from the script.

```yaml
version: 1
series: AI Survival
episode: 2
fps: 30
frames:
  - id: m-receipt                # cold open, "Then the bill shows up."
    kind: motion
    template: receipt-unroll
    start: 0
    vars:
      header: "SANDWICH STAND, AI BILL"
      lines:
        - "Reread policy binder, 40 pages"
        - "Reread policy binder, again"
        - "Read all 10,000 rows"
        - "Instructions, again"
        - "Big model, easy question"
      total_label: "TOTAL:"
      total: 150000
      unit: "TOKENS"
      source: "Example from a major AI lab, Nov 2025"
  - id: m-plan                   # section 3 preview, again at section 10
    kind: motion
    template: kinetic
    start: 12
    vars:
      label: "THE PLAN"
      lines:
        - "Find the waste."
        - "Trim what it reads."
        - "Move right-answer work into code."
        - "Right-size the model."
      amber_word: code
      subline: "Then test it before it replaces anything."
  - id: m-countup                # section 7, "the trick"
    kind: motion
    template: count-up
    start: 24
    vars:
      label: "ONE TASK, BEFORE AND AFTER"
      title: "Tokens for one task, before and *after*"
      before_label: "BEFORE: THE MODEL READS ALL 10,000 ROWS"
      after_label: "AFTER: CODE FILTERS, THE MODEL READS 5"
      before: 150000
      after: 2000
      unit: tokens
      tag: "98.7% fewer tokens"
      source: "Example from a major AI lab, Nov 2025"
  - id: m-slip                   # section 11 payoff gag
    kind: motion
    template: receipt-unroll
    start: 36
    vars:
      header: "SANDWICH STAND, NEW BILL"   # on screen from frame 1
      lines: ["Read 5 rows"]
      total_label: "TOTAL:"
      total: 2000
      unit: "TOKENS"
      source: "Example from a major AI lab, Nov 2025"
  - {id: m-ch0, kind: motion, template: chapter-card, start: 48, vars: {title: "How the *bill* works"}}
  - {id: m-ch1, kind: motion, template: chapter-card, start: 56, vars: {title: "Step one: *Find* the waste"}}
  - {id: m-ch2, kind: motion, template: chapter-card, start: 64, vars: {title: "Step two: *Trim* what it reads"}}
  - {id: m-ch3, kind: motion, template: chapter-card, start: 72, vars: {title: "Step three: Give right-answer work to *code*"}}
  - {id: m-ch4, kind: motion, template: chapter-card, start: 80, vars: {title: "The trick: Have the AI *write* the code"}}
  - {id: m-ch5, kind: motion, template: chapter-card, start: 88, vars: {title: "Step four: *Right-size* the model"}}
  - {id: m-ch6, kind: motion, template: chapter-card, start: 96, vars: {title: "*Test* it before it replaces anything"}}
  - {id: m-ch7, kind: motion, template: chapter-card, start: 104, vars: {title: "Where to *start* this week"}}
```

Notes on the Galley copy:

* Galley marks no amber word for the kinetic lines; `amber_word: code` is
  Studio's pick.
* No dollar amounts anywhere; the only figures are the sourced 150,000 and 2,000.
* In 16:9 `safe` the receipt is 608px wide, so longer lines wrap to two rows at
  40px; all lines still show at the total.

## Tests

`tests/ytp/test_motion.py` runs against `tests/ytp/fixtures/mock_hyperframes.py`
(a fake CLI that draws ink plus a box with ffmpeg) and needs Pillow and ffmpeg;
it skips without Pillow.
