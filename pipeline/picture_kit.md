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

- `overlay`: gold kicker, 2-line white headline, icon name. **Only** Point 1–3 sub talking points.
- `pip`: left-type (gold number/kicker, white sub, optional quote) plus a still query
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
Gold all-caps kicker, 2-line white headline, 72px gold line-art icon square.
No third font, no extra colors, no TAKEAWAY pills.

### pip

Full 16:9 still fills the frame (DVIDS named-platform file first).
Nano Banana 2 is a stub that may fill the **image slot only**. Prefer skip PiP if there is no still.
Host: entire talking-head frame scaled into a 560x315 16:9 window, margin 40, radius 16, gold 3px border.
Left third type over a dark left-to-right gradient: gold kicker, white sub, optional quote.

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
