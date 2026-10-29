# How Scott's notes become rules

Booth keeps one rules file per series. AI Survival lives in [`rules/ai-survival.yaml`](../rules/ai-survival.yaml). Each file has four sections. The owner is the room that has to honor the line:

| Section | Owner | What belongs here |
|---|---|---|
| `picture` | Studio | Host, cards, B-roll, shorts frame, thumbnails |
| `audio` | Track | Voice, loudness, music level, sound effects |
| `edit` | Booth | Cuts, splices, what may be named on camera |
| `script_copy` | Galley | Hook, bio, institution names, description, handle |

The 2026-10-27 seed and the 2026-10-27 / 2026-10-29 Booth handoff are already loaded into `rules/ai-survival.yaml`. Where they disagree, the later handoff wins. The bio line is the word-for-word Pentagon sentence, and it does not say "former".

## Add a note

When Scott says something in the room, add it the same day. Do not edit the YAML by hand unless you are changing a number in the `lint` block.

```bash
python -m ytp rule add --series ai-survival --section edit --text "Never say \"widget\"."
```

That appends the next id (`edit-005`, then `edit-006`, and so on) with today's date and the source `ytp rule add`. The other sections and the `lint` block stay put. Run it again for a second note. A new series name creates `rules/<series>.yaml` and starts it with the channel vendor ban.

```bash
python -m ytp rule list --series ai-survival
python -m ytp rule list --series ai-survival --section script_copy
```

`--section` is one of `picture`, `audio`, `edit`, `script_copy`.

## What the check enforces by itself

`ytp check VIDEO --plan PLAN --series ai-survival` reads the rules file and writes `qc/check-report.md` and `qc/check-report.json`. The process exits 1 when the report fails. It fails a render when it finds any of these:

- A quoted ban in a sentence that says never, do not, banned, or forbid. `Never say "widget".` becomes a banned phrase. Words after `instead` are the replacement, not a ban, so `Say "some vendors" instead.` does not ban that line.
- A parenthetical vendor list on a sentence that says not to name vendors. The on-camera line is **some vendors**.
- The `lint` block: banned phrases, vendor names, banned sound-effect ids, words per minute (175–185), loudness (voice -16 LUFS, Shorts -14 LUFS, true peak at or below -1.5 dBTP, music about 17 dB under the voice), em dashes, and emoji.

Write a new ban in that quoted form when Scott wants the checker to catch it. Pace and loudness numbers live under `lint` because they are measurements, not sentences.

Banned sounds are matched against music-cue ids (the sound name), not against the narration. The spoken word "buzz" in the picnic line is allowed. A cue whose id contains `buzz` or `film clip` is not.

## What stays a human read

The 4P hook, thumbnail wording, "choose the shot after the cut", and "credit outside claims" stay as prose. The checker does not try to grade those. Galley and Studio still read them.

## Draft only

Rules, renders, and checks stay on the machine. Nothing in this pipeline publishes a video or edits the YouTube listing.
