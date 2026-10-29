"""Validate the plan, render a draft, and print the check report.

The splice check stays on CPU. ``encoder=auto`` picks h264_nvenc only when
ffmpeg lists it and a one-frame encode succeeds.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from ytp.check import check_render, load_asr, measure_loudness, write_report
from ytp.plan import load_plan, validate_plan
from ytp.render import allocate_render_path, render_plan
from ytp.rules import load_rules


def finish_episode(
    episode_dir: Path,
    *,
    rules_root: Path | None = None,
    master: bool = True,
    denoise: bool = False,
) -> int:
    episode_dir = episode_dir.resolve()
    plan_path = episode_dir / "edit" / "plan.yaml"
    if not plan_path.is_file():
        print(f"ytp: no plan at {plan_path}", file=sys.stderr)
        return 1
    plan = load_plan(plan_path)
    errors = validate_plan(plan)
    for error in errors:
        print(f"ytp: {error}", file=sys.stderr)
    if errors:
        return 1
    out = allocate_render_path(episode_dir, f"{plan_path.stem}.mp4")
    result = render_plan(
        plan,
        out,
        episode_dir=episode_dir,
        plan_path=plan_path,
        encoder="auto",
        preset="veryfast",
        master=master,
        denoise=denoise,
        rules_root=rules_root,
    )
    print(f"wrote {result.video}")
    for line in result.logs:
        print(line)
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    words = episode_dir / "edit" / "words.json"
    asr = load_asr(words) if words.is_file() else None
    language = "en"
    try:
        language = str(load_rules(plan.series, rules_root).lint.get("language") or "en")
    except FileNotFoundError:
        language = "en"
    transcriber = _cpu_transcriber(language)
    report = check_render(
        result.video,
        plan,
        series=plan.series,
        plan_path=plan_path,
        rules_root=rules_root,
        transcriber=transcriber,
        asr=asr,
        episode_dir=episode_dir,
        loudness_fn=measure_loudness,
    )
    markdown, payload = write_report(report, episode_dir / "qc")
    _copy_versioned_report(markdown, result.video)
    print("PASS" if report.passed else "FAIL")
    print(markdown.read_text(encoding="utf-8").replace("\u00b1", "+/-"))
    print(payload)
    return 0 if report.passed else 1


def _copy_versioned_report(markdown: Path, video: Path) -> None:
    """Keep one report per render. ``check-report.md`` stays the latest."""
    version = video.parent.name
    if re.fullmatch(r"v\d+", version) is None:
        return
    dest = markdown.with_name(f"check-report-{version}.md")
    dest.write_text(markdown.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")


def _cpu_transcriber(language: str):
    """Splice windows stay on CPU int8. CUDA is for ``ytp take`` only."""
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        print(
            "warning: faster-whisper is not installed; splice windows were not re-heard",
            file=sys.stderr,
        )
        return None
    from ytp.transcribe import FasterWhisperTranscriber

    return FasterWhisperTranscriber("base", "cpu", language, window_model="small")
