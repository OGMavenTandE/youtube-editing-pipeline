from __future__ import annotations

import pytest

from ytp.cli import main
from ytp.rules import (
    SECTIONS,
    add_rule,
    collected_bans,
    extract_banned_phrases,
    extract_vendors,
    load_rules,
)


def test_seed_covers_every_owner(rules_root) -> None:
    rules = load_rules("ai-survival", rules_root)
    assert set(rules.sections) == set(SECTIONS)
    assert rules.owners["picture"] == "Studio"
    assert rules.owners["audio"] == "Track"
    assert rules.owners["edit"] == "Booth"
    assert rules.owners["script_copy"] == "Galley"
    assert rules.sections["picture"]
    assert rules.sections["audio"]
    assert rules.sections["edit"]
    assert rules.sections["script_copy"]
    assert rules.lint["wpm"]["min"] == 175
    assert rules.lint["wpm"]["max"] == 185
    assert rules.lint["loudness"]["voice_lufs"] == -16
    assert rules.lint["loudness"]["shorts_lufs"] == -14
    assert rules.lint["loudness"]["true_peak_dbtp"] == -1.5
    assert rules.lint["loudness"]["music_under_voice_db"] == 17


def test_seed_withdraws_former_bio_and_bans_vendors(rules_root) -> None:
    rules = load_rules("ai-survival", rules_root)
    copy = " ".join(rule.text for rule in rules.sections["script_copy"])
    assert "I manage AI test and evaluation programs for the Pentagon" in copy
    assert "He manages AI test and evaluation programs for the Pentagon" in copy
    assert "withdrawn" in copy
    bans = collected_bans(rules)
    for name in ("Grok", "Google", "Anthropic", "ChatGPT", "OpenAI", "Claude", "Gemini"):
        assert name in bans.vendors
    assert "some vendors" == bans.say_instead
    assert "former" in bans.phrases
    assert "stick figure" in bans.phrases
    assert "@scottmastin" in bans.phrases
    assert "like and subscribe" in bans.phrases
    assert "buzz" in bans.sounds


def test_quoted_ban_ignores_the_replacement_line() -> None:
    text = 'Never say "ChatGPT". Say "some vendors" instead.'
    assert extract_banned_phrases(text) == ["ChatGPT"]
    vendors = extract_vendors(
        'Never name a vendor on camera (Grok, Google, Anthropic). Say "some vendors" instead.'
    )
    assert vendors == ["Grok", "Google", "Anthropic"]


def test_rule_add_appends_without_dropping_seed(rules_root) -> None:
    before = load_rules("ai-survival", rules_root)
    added = add_rule(
        "ai-survival",
        "edit",
        'Never say "widget".',
        root=rules_root,
    )
    after = load_rules("ai-survival", rules_root)
    assert added.id == "edit-005"
    assert [rule.id for rule in after.sections["edit"]].count("edit-005") == 1
    assert len(after.sections["picture"]) == len(before.sections["picture"])
    assert after.lint["loudness"]["voice_lufs"] == -16
    assert "widget" in collected_bans(after).phrases
    assert "former" in collected_bans(after).phrases


def test_rule_add_cli_and_rejects_bad_section(rules_root, capsys) -> None:
    code = main(
        [
            "rule",
            "add",
            "--series",
            "ai-survival",
            "--section",
            "script_copy",
            "--text",
            'Never say "widget".',
            "--rules-root",
            str(rules_root),
        ]
    )
    assert code == 0
    listed = main(
        [
            "rule",
            "list",
            "--series",
            "ai-survival",
            "--section",
            "script_copy",
            "--rules-root",
            str(rules_root),
        ]
    )
    assert listed == 0
    output = capsys.readouterr().out
    assert "widget" in output
    assert "Galley" in output
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "rule",
                "add",
                "--series",
                "ai-survival",
                "--section",
                "camera",
                "--text",
                "nope",
                "--rules-root",
                str(rules_root),
            ]
        )
    assert exc.value.code == 2


def test_new_series_file_includes_vendor_ban(rules_root) -> None:
    add_rule("desk-notes", "picture", "Face PiP stays lower right.", root=rules_root)
    rules = load_rules("desk-notes", rules_root)
    assert "Grok" in collected_bans(rules).vendors
    assert rules.sections["picture"][0].text.startswith("Face PiP")
