# Scene spec (`ytp scenes`)

`ytp scenes` turns a small YAML or JSON file into finished 1920x1080 PNG frames
in Ep01's look, then writes the matching still segments for the edit plan so
`ytp finish` can assemble the episode with no hand edits.

```bash
python -m ytp scenes ep02/edit/scenes.yaml               # frames/ + frames/scenes-plan.yaml
python -m ytp scenes ep02/edit/scenes.yaml --plan        # also merge into ep02/edit/plan.yaml
python -m ytp scenes ep02/edit/scenes.yaml --only 02 --safe-guides
python -m ytp finish ep02
```

| Option | Meaning |
| --- | --- |
| `--episode DIR` | Episode folder. Default: the spec's folder, or its parent when the spec sits in `edit/`. |
| `--out DIR` | Where frames go. Default `<episode>/frames`. |
| `--plan [PATH]` | Merge the segments into an edit plan. Bare `--plan` means `<episode>/edit/plan.yaml`. Without it, the segments go to `<out>/scenes-plan.yaml`. |
| `--only ID` | Render one frame (repeatable). Handy while Studio iterates on a single plate. |
| `--safe-guides` | Also write debug copies to `<out>/guides/` with the 9:16 safe area outlined and the outside shaded. The real frames stay clean. |
| `--no-clips` | Skip count-up clips; stills only. |

Art never goes in the repo. The spec names asset ids, and each id maps to a
file on the PC (see [Assets](#assets)). `assets/scenes/` is gitignored.

## Look (what Ep01's comp.py did, now the defaults)

| Piece | Default |
| --- | --- |
| Font | Inter at weight 900, optical size 32 (Inter Black). Not bundled; see [Fonts](#fonts). |
| Plate | Scaled to fill 1920x1080 and center-cropped. `plate: {trim: true}` first cuts near-white borders, as Ep01's generated art needed. |
| Big word | Centered at x 960, y 150, black stroke of size/14. 200 px when shorter than 9 characters, else 160 px. Bright palette `#FFD23F`, dark `#FF8A2B`. |
| Caption | One line, white on a black rounded pill centered at y 990. 64 px when shorter than 40 characters, else 50 px; pill height 1.6x the size, 40 px side padding, radius 24. Shrinks only if it would run off the frame. |
| Tags | Red `#E63946` rounded boxes with a 5 px black outline, white 56 px text (Ep01's PERSON 94% and HAT 80%). |
| End card | Big text white at (620, 480), 150 px, stroke 11. |

With `layout: {big: {fit: fixed}}` and the frame table from comp.py, `ytp
scenes` reproduces Ep01's 24 frames pixel for pixel (same Pillow version).

## Palettes and accent

Two built-in palettes: `bright` for tip beats and `dark` for threat and joke
beats. Each has these colors, and a spec can override any of them:

```yaml
palettes:
  bright: {big: "#FFD23F", text: "#FFFFFF", stroke: "#000000", pill: "#000000",
           label: "#E63946", label_text: "#FFFFFF", strike: "#E63946",
           source: "#FFFFFF", card: "#14213D", endcard: "#FFFFFF"}
  dark:   {big: "#FF8A2B", card: "#111111"}   # the rest as bright
```

A frame's `accent: "#E63946"` replaces the big word color for that frame only,
for example red for the coin meter or a stamp beat. New palettes can be added
by name under `palettes:`.

## Frames

Animated frames (`kind: motion`: count-up, kinetic type, chapter cards,
receipt) render through HyperFrames; see [motion.md](motion.md).

```yaml
frames:
  - id: "02"                 # quote ids so YAML keeps leading zeros
    kind: key                # key | pause | card | endcard (key and pause look the same)
    art: plate-stand         # background plate asset id; optional only for kind: card
    layers: [...]            # art on top of the plate, see below
    palette: bright          # bright | dark | any name under palettes
    accent: "#E63946"        # optional big word color override
    big: "2,000"             # one word or number
    strike: false            # draw a line through the big word
    was: "150,000"           # optional smaller struck-through value above the big one
    count: {...}             # optional count-up or count-down on the big number
    caption: One line.       # optional
    source: "Source: ..."    # optional small line in a bottom corner
    source_corner: right     # right (default) | left
    labels: [{text: "PERSON 94%", x: 900, y: 300}]   # or box: [x1, y1, x2, y2]
    big_x: 960               # optional placement override (also big_y, big_size)
    start: 14.656            # seconds; or line: <narration id>; TBD leaves it out of the plan
    end: 18.0                # optional; normally the next frame's start
    duration: 3.5            # optional alternative to end
    beat: 2
    motion: {zoom: 1.0->1.08, pan: left}   # optional Ken Burns in ytp render
    file: ep02-02-b1-key.png # optional output name; default <id>.png
```

`kind: card` is a text card with no art: the background is the palette's
`card` color and the big word sits mid-frame (y 470, up to 260 px).
`kind: endcard` draws the big text in Ep01's end-card spot.

### Layers

A frame is one background plate plus zero or more art layers, drawn in `z`
order (low first) with their alpha kept:

| Key | Meaning |
| --- | --- |
| `asset` | Asset id. |
| `x`, `y` | Position in frame pixels. Default anchor is the layer's center. |
| `anchor` | `center` (default), `topleft`, or `bottom` (feet on `y`, good for host poses). |
| `scale` | Multiplier on the PNG's own size. |
| `flip` | `true` or `h` mirrors left-right; `v` flips upside down; `hv` both. |
| `z` | Draw order. Text always goes on top of every layer. |
| `role` | `prop` (default), `host`, or `text`. Host and text layers are checked against the safe area. |
| `opacity` | 0 to 1. |

Layers may hang off the edge of the frame; the off-screen part is cut.

### Big word fit and the 9:16 safe area

Shorts cut a centered 9:16 slice from the 16:9 frame: 608 px wide, x 656 to
1264 on 1920x1080. By default (`layout.big.fit: safe`) the big word starts at
Ep01's size and shrinks in 2 px steps until it fits inside that slice with a
24 px margin, never below 72 px. Long words therefore get smaller than Ep01's;
set `layout: {big: {fit: fixed}}` for Ep01's fixed sizes, or `big_size` on one
frame.

`ytp scenes` prints a warning when the big word, the struck `was` value, or a
`host`/`text` layer falls outside the safe slice (`big_x` overrides usually
cause this). Captions and the source line are not checked: the caption is a
full-width subtitle the Shorts cut sets again, and the source line sits in a
corner on purpose. End cards are not checked. `--safe-guides` draws the slice
so Studio can see it.

### Strikethrough

`strike: true` puts a red bar (palette `strike`, black edge) through the big
word. `was: "150,000"` draws the old value at 55% size above the big word,
struck through, and moves the big word down under it. Use either, or both
across two frames for a before/after beat.

### Count-up and count-down

```yaml
count: {from: 150000, to: 2000, duration: 1.5, ease: out,
        decimals: 0, separator: true, prefix: "", suffix: ""}
```

The big number counts from `from` to `to` over `duration` seconds at the plan's
fps (30), with thousands separators by default. `ease` is `linear`, `out`
(fast then settling), or `inout`. `ytp scenes` writes `<id>-count.mp4` (H.264,
CRF 14) next to the still, and the still shows the final value. In the plan the
frame becomes a `clip` segment for the count followed by a `still` segment for
the rest of the frame's time. The text size is fixed from the wider of the two
values so it does not jump while counting.

Cost: each count frame is drawn with Pillow and piped to ffmpeg. A 1.5 s count
is 45 frames and takes about 1 to 2 seconds on the box. `ytp render` then
re-encodes it like any other clip with the plan's encoder.

### Ken Burns

`motion: {zoom: 1.0->1.08, pan: left|right|up|down|none}` on a frame becomes
`motion` on its still segment, and `ytp render` animates it. Off by default.
Zoom is linear from the first to the last frame. Pan drifts from the center
toward the named edge inside the room the zoom opens up, so a pan needs some
zoom (`1.08->1.08` pans with no zoom change). The still is upscaled 4x before
ffmpeg's `zoompan`, so crops move in quarter-pixel steps instead of shuddering.
GPU encode is unchanged: the filter runs on the CPU and the encoder stays the
plan's. A 4 s 1080p still takes about 2.5 s on the box. With a count, motion
applies to the still part after the count.

## Timing and the plan

A frame starts at `start`, or at the start of the narration line named by
`line` (looked up in the episode's `edit/plan.yaml`, or the plan given with
`--plan`). The picture is one straight concat, so each frame holds until the
next frame starts. The last frame ends at `end`, `start + duration`, its line's
end, or the end of the last narration line. A first frame that starts after 0
is held from 0. Frames with no start (`start: TBD`) are still drawn but left
out of the plan, with a warning.

Segments are named `scene-<id>` (and `scene-<id>-count` for a count clip).
`--plan` drops earlier `scene-` segments, keeps every other segment (talking
head clips, for example), sorts by start, and warns about overlaps. Sources are
written relative to the episode folder, e.g. `frames/02.png`.

## Assets

```yaml
asset_dirs: [art/ep02, ../shared-art]   # optional, relative to the episode
assets:
  plate-stand: plates/sandwich-stand.png
  host-wave: hosts/wave.png
  drone: props/drone.png
```

Absolute paths are used as given. Relative paths are looked up in this order,
first match wins (the same idea as the end card):

1. each folder in `asset_dirs` (relative to the episode folder)
2. the episode folder
3. the spec's folder
4. each folder in `YTP_SCENE_ASSETS` (`;`-separated on Windows, `:` elsewhere)
5. `<repo parent>\assets\scenes`, e.g. `C:\AI Eval\YouTube\assets\scenes`
6. `<episode>\assets\scenes`
7. the repo's own `assets/scenes` (gitignored, local development only)

A missing asset stops the run and lists every place it looked. Plates can be
any size; they are cover-cropped. Layers should be RGBA PNGs with transparent
backgrounds.

## Fonts

Nothing is bundled. `ytp scenes` looks for Inter in this order: `font.path` in
the spec (relative to the spec), `YTP_SCENE_FONT`, then the font folders
(`%WINDIR%\Fonts`, `%LOCALAPPDATA%\Microsoft\Windows\Fonts`, `~/Library/Fonts`,
`/usr/share/fonts`, `~/.local/share/fonts` and the like) for
`Inter-VariableFont_opsz,wght.ttf`, `InterVariable.ttf`, or `Inter-Black.ttf`.
Variable fonts are set to weight 900 and optical size 32 by axis name
(`font: {weight: 900, opsz: 32}` to change). If none is found the run stops and
says how to install Inter. On Windows: install the variable TTF from Google
Fonts or rsms.me/inter "for all users" or for yourself; either folder works.

## Full example (Ep02 cold open)

The asset ids below are Studio's v1 list (plates, host poses, props). The art
files stay on the PC.

```yaml
version: 1
size: [1920, 1080]
fps: 30
asset_dirs: [art]
assets:
  plate-stand: plates/sandwich-stand.png
  plate-street: plates/receipt-street.png
  plate-title: plates/title-dots.png
  host-wave: hosts/host-wave.png
  drone: props/drone.png
  coin-meter: props/coin-meter.png
  binder: props/policy-binder.png
  pickles: props/pickles.png
defaults:
  palette: bright
frames:
  # Joke beat: the drone delivers sandwiches now.
  - id: "01"
    kind: key
    palette: dark
    art: plate-stand
    layers:
      - {asset: host-wave, x: 960, y: 1000, anchor: bottom, scale: 0.9, z: 1, role: host}
      - {asset: drone, x: 1480, y: 300, scale: 0.7, flip: true, z: 2}
    big: SANDWICHES
    caption: It delivers sandwiches now.
    start: 0
    beat: 1
    motion: {zoom: 1.0->1.06, pan: right}

  # Before and after: 150,000 struck, counting down to 2,000.
  - id: "02"
    kind: pause
    art: plate-street
    layers:
      - {asset: coin-meter, x: 1500, y: 560, scale: 0.8, z: 1}
    accent: "#E63946"
    was: "150,000"
    count: {from: 150000, to: 2000, duration: 1.5, ease: out}
    caption: One task. 150,000 tokens down to 2,000.
    source: Sources in the description
    line: n05            # starts with that narration line
    beat: 1

  # Tip beat title card, no art.
  - id: "03"
    kind: card
    big: STEP 1
    caption: Find the waste.
    line: n12
    beat: 4

  # Strikethrough alone, with a source line and a host off to the side.
  - id: "04"
    kind: key
    palette: dark
    art: plate-stand
    layers:
      - {asset: binder, x: 700, y: 640, scale: 0.8, z: 1}
      - {asset: pickles, x: 1250, y: 700, scale: 0.6, z: 2}
    big: NO PICKLES
    strike: true
    caption: Buried on page twenty-two.
    source: "Liu et al., 2023, Lost in the Middle"
    source_corner: left
    line: n31
    beat: 5
```

## Ep01 in this format

Ep01's comp.py table maps row for row: `raw` becomes `art` (with
`asset_dirs: [frames/raw]` and `plate: {trim: true}`), the X overrides become
`big_x`, the PERSON and HAT tags become `labels`, and
`layout: {big: {fit: fixed}}` keeps comp.py's 200/160 sizes. Rendering that
spec matched all 23 readable reference frames exactly.
