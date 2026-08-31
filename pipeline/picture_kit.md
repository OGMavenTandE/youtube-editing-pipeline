# Scott Mastin picture kit

Locked look. The app tags and composites. It does not invent a layout, font, color, zoom, or Scott.

## Talk structure

1. Title + executive summary (spoken) over the open bookend (`open_card` + lower third)
2. Point 1 (PiP only if a named-platform still exists) with 2–3 spoken subpoints as overlay cards
3. Point 2, same
4. Point 3, same
5. Closing wrap (`close_card` + lower third, never a PiP) with contact

Most of the file is `nothing`: full-frame host, no chrome.

## Model contract

The model may tag a **body** beat as `overlay` | `pip` | `nothing` and fill copy:

- `overlay`: gold kicker (talk-sheet card title), 2-line white headline, icon name. **Only** Point 1–3 sub talking points.
- `pip`: still plate matching overlay cards (optional gold image title, white image text), plus a still query
- `nothing`: no copy

It must not choose layout, font, colors, zoom, or `lower_third`.
It must not generate Scott. Sparse: overlay is the default markup; PiP is rare; most beats are `nothing`.

`open_card` (title kicker + two-line thesis) and `close_card` (locked CTA) are talk-sheet / job metadata. The app applies them with the lower third at open and close. They are not body tags. Do not take `open_card` from Point 1.

## Visual constants

One font: Inter (bundled under `pipeline/fonts/`).
Two colors: white `#FFFFFF`, gold `#E0B44A`.
Dark plate: rgb(8,10,14) at alpha ~200.
Host is always the real camera. Never a generated face. Never a full-screen wipe except the PiP window, which is the entire 1920x1080 talking-head frame scaled (not a face crop).

### overlay

Host stays full 1920x1080.
Top-left plate: x=56 y=48 w=620 radius 22 pad 32.
Gold all-caps kicker (one short line, ~40 characters), 2-line white headline that fits the 556px plate (~76 characters). Long copy truncates on the plate. Never mid-word overflow.
Card Title[j] paints with Card body[j] of the same card. No cross-card zip.
No third font, no extra colors, no TAKEAWAY pills.

### pip

Full 16:9 still fills the frame (DVIDS named-platform file first).
Nano Banana 2 is a stub that may fill the **image slot only**. Prefer skip PiP if there is no still.
Host: entire talking-head frame scaled into a 560x315 16:9 window, margin 40, radius 16, gold 3px border.
Same Nate plate as overlay cards: dark rounded box, Inter, gold `#E0B44A` image title (optional), white `#FFFFFF` image text. Wrap. Stay on-frame. Do not cover the 560×315 bottom-right cam window. Empty image title means white content only. Never invent a title by splitting image text on a period or newline. One field, one role. No left-third gradient-only treatment.
PiP / still beats hold `PIP_HOLD_SECONDS` (default 8s) so the title can be read twice, then a hard cut back to full-frame host. Do not grow into adjacent `nothing` past that cap. Overlay duration stays put. Still image title and image text never fill an overlay-card kicker.

### bookends (open + close)

Structural. First ~8–12s and last ~8–12s. Not a model choice. Never a PiP.

Two chromes at once:

1. Dedicated bookend plate (same Nate chrome as body overlays, different field).
   - `open_card`: gold kicker = video title; 2-line white headline = thesis from the talk sheet. Not Point 1.
   - `close_card`: locked CTA, kicker `WORK WITH ME`, headline `Independent AI T&E.` / `Vendor-agnostic.`
2. Lower third, ~220px, two columns, gold 4–6px top rule, side margin 48.
   - Left (locked identity, every video): name / title line / affiliations in gold / mission.
   - Right: `FIND ME` plus site, LinkedIn, aieval.org.
   - No on-screen WRAP kicker.

Identity and find-me strings are config. Title / exec / close-card copy come from the talk sheet / job metadata.

## Persist

Write `output/<stem>_tagged_beats.json` after tagging and bookends, before encode, so a retry skips the model.

The Windows tray form writes `<stem>_talk_sheet.json` (open title, overview, Point 1–3 platform/still/image title/image text/card titles/card headlines, source flags). Close and identity stay locked. Empty slots may auto-fill a short image title from the platform or still name, and image text from the point. Auto copy never invents a directive or says DoD. Auto-fill never splits a user-typed image text string. User slots do not get rewritten.
