"""Pass/fail report for a rendered episode.

Checks caption timing, integrated loudness and true peak, words per minute,
banned words and sound-effect ids from the series rules, and the splice
windows. Splice windows are re-transcribed. A leftover fragment fails the cut.
"""

from __future__ import annotations

import json
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ytp.captions import format_timestamp
from ytp.ff import has_audio, probe_duration, probe_video_size, run_ffmpeg
from ytp.plan import EditPlan
from ytp.rules import SeriesRules, collected_bans, load_rules
from ytp.splice import (
    compare_window,
    narration_word_times,
    normalize_token,
    splice_times,
    tokenize,
    words_overlapping,
)
from ytp.transcribe import Transcript, Word, extract_audio

CAPTION_LEAD = 0.40
CAPTION_TRAIL = 0.50
DURATION_TOLERANCE = 0.75
_EMOJI = re.compile("[\U0001f300-\U0001faff\U00002700-\U000027bf\U00002600-\U000026ff]")
_LOUDNESS_JSON = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.S)


@dataclass
class Finding:
    check: str
    severity: str
    code: str
    message: str
    timestamp: float | None = None
    word: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "check": self.check,
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
        }
        if self.timestamp is not None:
            payload["timestamp"] = round(self.timestamp, 3)
            payload["timestamp_text"] = format_timestamp(self.timestamp)
        if self.word is not None:
            payload["word"] = self.word
        return payload


@dataclass
class CheckResult:
    id: str
    passed: bool
    summary: str
    findings: list[Finding] = field(default_factory=list)
    skipped: bool = False
    measurements: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "passed": self.passed,
            "skipped": self.skipped,
            "summary": self.summary,
            "measurements": self.measurements,
            "findings": [finding.to_dict() for finding in self.findings],
        }


@dataclass
class Report:
    video: str
    plan: str
    series: str
    passed: bool
    checks: list[CheckResult]

    def to_dict(self) -> dict[str, object]:
        return {
            "video": self.video,
            "plan": self.plan,
            "series": self.series,
            "passed": self.passed,
            "checks": [check.to_dict() for check in self.checks],
        }

    def to_markdown(self) -> str:
        lines = [
            "# Render check",
            "",
            f"- result: {'PASS' if self.passed else 'FAIL'}",
            f"- video: {self.video}",
            f"- plan: {self.plan}",
            f"- series: {self.series}",
            "",
        ]
        for check in self.checks:
            if check.skipped:
                status = "SKIP"
            else:
                status = "PASS" if check.passed else "FAIL"
            lines.append(f"## {check.id} — {status}")
            lines.append("")
            lines.append(check.summary)
            lines.append("")
            if check.findings:
                lines.append("| time | code | detail |")
                lines.append("|---|---|---|")
                for finding in check.findings:
                    if finding.timestamp is None:
                        when = ""
                    else:
                        when = format_timestamp(finding.timestamp)
                    detail = finding.message.replace("|", "\\|")
                    lines.append(f"| {when} | {finding.code} | {detail} |")
                lines.append("")
        return "\n".join(lines) + "\n"


def write_report(report: Report, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    markdown = out_dir / "check-report.md"
    payload = out_dir / "check-report.json"
    markdown.write_text(report.to_markdown(), encoding="utf-8", newline="\n")
    body = json.dumps(report.to_dict(), indent=2) + "\n"
    payload.write_text(body, encoding="utf-8", newline="\n")
    return markdown, payload


def measure_loudness(path: Path) -> tuple[float, float]:
    """Integrated LUFS and true peak (dBTP) from ffmpeg loudnorm's input measurement."""
    proc = run_ffmpeg(
        [
            "-i",
            str(path),
            "-af",
            "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json",
            "-f",
            "null",
            "-",
        ]
    )
    match = _LOUDNESS_JSON.search(proc.stderr or "")
    if match is None:
        raise RuntimeError(f"loudnorm printed no measurement for {path}")
    data = json.loads(match.group(0))
    return float(data["input_i"]), float(data["input_tp"])


def music_under_db(voice_lufs: float, music_lufs: float, gain_db: float) -> float:
    """How many dB the cue sits under the voice after gain. About 17 is the rule."""
    return voice_lufs - (music_lufs + gain_db)


def speaking_wpm(plan: EditPlan) -> float | None:
    words = 0
    seconds = 0.0
    for line in plan.narration:
        words += len(tokenize(line.text))
        seconds += max(0.0, line.end - line.start)
    if words == 0 or seconds <= 0:
        return None
    return words / (seconds / 60.0)


def _is_json_transcript(raw: str, path: Path) -> bool:
    if path.suffix.lower() == ".json":
        return True
    stripped = raw.lstrip()
    if stripped.startswith("{"):
        return True
    # A JSON array of word objects, not the bracket transcript `[ 0.00- 5.24]`.
    return stripped.startswith("[") and len(stripped) > 1 and stripped[1] in "{[\"'"


def load_asr(path: Path) -> Transcript:
    """Word JSON, or the Ep01 bracket lines ``[start-end] text``."""
    raw = path.read_text(encoding="utf-8")
    if _is_json_transcript(raw, path):
        data = json.loads(raw)
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            rows = data.get("words") or data.get("timestamps") or []
        else:
            rows = []
        words = [
            Word(
                str(item.get("word") or item.get("text") or ""),
                float(item["start"]),
                float(item["end"]),
            )
            for item in rows
        ]
        return Transcript(text=" ".join(word.text for word in words), words=words)
    words: list[Word] = []
    pieces: list[str] = []
    pattern = re.compile(r"^\[\s*([0-9.]+)\s*-\s*([0-9.]+)\s*\]\s*(.*)$")
    for line in raw.splitlines():
        match = pattern.match(line.strip())
        if not match:
            continue
        start = float(match.group(1))
        end = float(match.group(2))
        sentence = match.group(3).strip()
        pieces.append(sentence)
        tokens = tokenize(sentence)
        if not tokens:
            continue
        span = max(end - start, 1e-3)
        step = span / len(tokens)
        for index, token in enumerate(tokens):
            word_start = start + step * index
            words.append(Word(token, word_start, word_start + step))
    return Transcript(text=" ".join(pieces), words=words)


def _fail(
    check: str,
    code: str,
    message: str,
    timestamp: float | None = None,
    word: str | None = None,
) -> Finding:
    return Finding(check, "fail", code, message, timestamp, word)


def _contains(text: str, phrase: str) -> bool:
    if not phrase or not text:
        return False
    pattern = r"(?<![A-Za-z0-9])" + re.escape(phrase) + r"(?![A-Za-z0-9])"
    return re.search(pattern, text, flags=re.I) is not None


def _dedupe(findings: list[Finding]) -> list[Finding]:
    seen: set[tuple[str, str, str, float | None]] = set()
    kept: list[Finding] = []
    for finding in findings:
        stamp = None if finding.timestamp is None else round(finding.timestamp, 2)
        key = (finding.check, finding.code, (finding.word or finding.message).casefold(), stamp)
        if key in seen:
            continue
        seen.add(key)
        kept.append(finding)
    return kept


def check_render(
    video: Path,
    plan: EditPlan,
    *,
    series: str,
    plan_path: Path | None = None,
    rules_root: Path | None = None,
    transcriber=None,
    asr: Transcript | None = None,
    window: float | None = None,
    profile: str = "auto",
    loudness_fn: Callable[[Path], tuple[float, float]] = measure_loudness,
    episode_dir: Path | None = None,
) -> Report:
    rules = load_rules(series, rules_root)
    width, height = probe_video_size(video)
    duration = probe_duration(video)
    use_short = profile == "short" or (profile == "auto" and height > width)
    checks = [
        _check_container(video, plan, width, height, duration, use_short),
        _check_loudness(video, plan, rules, use_short, loudness_fn, episode_dir),
        _check_wpm(plan, rules),
        _check_banned(plan, rules, asr),
        _check_captions(plan, asr),
        _check_splices(video, plan, rules, transcriber, asr, window),
    ]
    passed = all(check.passed or check.skipped for check in checks)
    return Report(
        video=str(video),
        plan=str(plan_path or ""),
        series=series,
        passed=passed,
        checks=checks,
    )


def _check_container(video, plan, width, height, duration, use_short: bool) -> CheckResult:
    findings: list[Finding] = []
    expected = f"{plan.width}x{plan.height}"
    if use_short:
        allowed = {(1080, 1920)}
        allowed.update((short.width, short.height) for short in plan.shorts)
        if (width, height) not in allowed and (width, height) != (plan.width, plan.height):
            findings.append(
                _fail(
                    "container",
                    "resolution",
                    f"short is {width}x{height}; expected 1080x1920",
                )
            )
    elif (width, height) != (plan.width, plan.height):
        findings.append(
            _fail(
                "container",
                "resolution",
                f"video is {width}x{height}; plan is {expected}",
            )
        )
    target = plan.picture_duration()
    if not use_short and target and abs(duration - target) > DURATION_TOLERANCE:
        findings.append(
            _fail(
                "container",
                "duration",
                f"video is {duration:.3f}s; plan picture is {target:.3f}s",
            )
        )
    summary = f"{width}x{height}, {duration:.3f}s"
    return CheckResult(
        "container",
        not findings,
        summary,
        findings,
        measurements={"duration": duration},
    )


def _check_loudness(video, plan, rules, use_short, loudness_fn, episode_dir) -> CheckResult:
    loud = rules.lint.get("loudness") or {}
    target = float(loud.get("shorts_lufs") if use_short else loud.get("voice_lufs"))
    tolerance = float(loud.get("lufs_tolerance", 1))
    peak_limit = float(loud.get("true_peak_dbtp", -1.5))
    music_target = float(loud.get("music_under_voice_db", 17))
    music_tol = float(loud.get("music_tolerance_db", 3))
    findings: list[Finding] = []
    measurements: dict[str, object] = {"target_lufs": target, "true_peak_limit": peak_limit}
    try:
        integrated, true_peak = loudness_fn(video)
    except Exception as exc:
        return CheckResult(
            "loudness",
            False,
            f"could not measure loudness: {exc}",
            [_fail("loudness", "measure", str(exc))],
        )
    measurements["integrated_lufs"] = integrated
    measurements["true_peak_dbtp"] = true_peak
    if abs(integrated - target) > tolerance:
        findings.append(
            _fail(
                "loudness",
                "integrated_lufs",
                (
                    f"integrated loudness is {integrated:.2f} LUFS; "
                    f"target {target:.1f} +/- {tolerance:.1f}"
                ),
            )
        )
    if true_peak > peak_limit:
        findings.append(
            _fail(
                "loudness",
                "true_peak",
                f"true peak is {true_peak:.2f} dBTP; limit is {peak_limit:.1f}",
            )
        )
    music_notes = _check_music_level(
        plan, episode_dir, loudness_fn, music_target, music_tol, findings, measurements
    )
    summary = f"{integrated:.2f} LUFS, true peak {true_peak:.2f} dBTP"
    if music_notes:
        summary = f"{summary}. {music_notes}"
    return CheckResult("loudness", not findings, summary, findings, measurements=measurements)


def _resolve(episode_dir: Path | None, value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    if path.is_file():
        return path
    if episode_dir is not None and (episode_dir / value).is_file():
        return episode_dir / value
    return None


def _check_music_level(
    plan,
    episode_dir,
    loudness_fn,
    target,
    tolerance,
    findings,
    measurements,
) -> str:
    voice = _resolve(episode_dir, plan.voice)
    cues = []
    for cue in plan.music:
        source = _resolve(episode_dir, cue.source)
        if source is not None:
            cues.append((cue, source))
    if voice is None or not cues:
        measurements["music_under_voice"] = "skipped"
        return "music-under-voice skipped (no voice stem and cue files together)"
    try:
        voice_lufs, _peak = loudness_fn(voice)
    except Exception as exc:
        findings.append(_fail("loudness", "voice_stem", f"could not measure voice stem: {exc}"))
        return "voice stem could not be measured"
    unders = []
    for cue, source in cues:
        try:
            music_lufs, _peak = loudness_fn(source)
        except Exception as exc:
            findings.append(_fail("loudness", "music_stem", f"{cue.id}: {exc}", cue.at))
            continue
        under = music_under_db(voice_lufs, music_lufs, cue.gain_db)
        unders.append(under)
        measurements[f"under_db:{cue.id}"] = round(under, 2)
        if abs(under - target) > tolerance:
            findings.append(
                _fail(
                    "loudness",
                    "music_level",
                    (
                        f"{cue.sfx} is {under:.1f} dB under the voice; "
                        f"target about {target:.0f} +/- {tolerance:.0f}"
                    ),
                    cue.at,
                )
            )
    if not unders:
        return "music stems could not be measured"
    return f"music under voice {unders[0]:.1f} dB (target {target:.0f})"


def _check_wpm(plan: EditPlan, rules: SeriesRules) -> CheckResult:
    band = rules.lint.get("wpm") or {}
    low = float(band.get("min", 175))
    high = float(band.get("max", 185))
    pace = speaking_wpm(plan)
    if pace is None:
        return CheckResult("wpm", True, "no timed narration", skipped=True)
    findings = []
    if pace < low or pace > high:
        findings.append(
            _fail("wpm", "pace", f"speaking rate is {pace:.1f} wpm; target {low:.0f}-{high:.0f}")
        )
    return CheckResult(
        "wpm",
        not findings,
        f"{pace:.1f} wpm (target {low:.0f}-{high:.0f})",
        findings,
        measurements={"wpm": round(pace, 2)},
    )


def _check_banned(plan: EditPlan, rules: SeriesRules, asr: Transcript | None) -> CheckResult:
    bans = collected_bans(rules)
    findings: list[Finding] = []
    blobs: list[tuple[str, float | None]] = []
    for line in plan.narration:
        blobs.append((line.text, line.start))
    for cue in plan.captions:
        blobs.append((cue.text, cue.start))
    for short in plan.shorts:
        blobs.append((short.title, short.start))
    for segment in plan.segments:
        if segment.template_text:
            blobs.append((segment.template_text, segment.start))
    phrases = list(bans.phrases) + list(bans.vendors)
    for text, stamp in blobs:
        for phrase in phrases:
            if _contains(text, phrase):
                replacement = ""
                vendor_names = {name.casefold() for name in bans.vendors}
                if phrase in bans.vendors or phrase.casefold() in vendor_names:
                    replacement = f' Say "{bans.say_instead}" instead.'
                findings.append(
                    _fail(
                        "banned",
                        "banned_phrase",
                        f'found "{phrase}".{replacement}',
                        stamp,
                        phrase,
                    )
                )
        if bans.ban_em_dash and "\u2014" in text:
            findings.append(_fail("banned", "em_dash", "em dash in copy", stamp, "\u2014"))
        if bans.ban_emoji and _EMOJI.search(text):
            findings.append(_fail("banned", "emoji", "emoji in copy", stamp))
    if asr is not None:
        for phrase in phrases:
            for stamp in _phrase_times(asr.words, phrase):
                findings.append(
                    _fail(
                        "banned",
                        "banned_phrase",
                        f'narration audio contains "{phrase}"',
                        stamp,
                        phrase,
                    )
                )
    for cue in plan.music:
        name = cue.sfx.casefold()
        for sound in bans.sounds:
            if sound.casefold() in name:
                findings.append(
                    _fail(
                        "banned",
                        "banned_sound",
                        f'sound id "{cue.sfx}" matches banned "{sound}"',
                        cue.at,
                        cue.sfx,
                    )
                )
                break
    findings = _dedupe(findings)
    summary = "no banned words or sounds" if not findings else f"{len(findings)} banned hit(s)"
    return CheckResult("banned", not findings, summary, findings)


def _phrase_times(words: list[Word], phrase: str) -> list[float]:
    tokens = [token for token in (normalize_token(part) for part in tokenize(phrase)) if token]
    if not tokens:
        return []
    normalized = [normalize_token(word.text) for word in words]
    found = []
    last = len(normalized) - len(tokens) + 1
    for index in range(max(0, last)):
        if normalized[index : index + len(tokens)] == tokens:
            found.append(words[index].start)
    return found


def _speech_words(plan: EditPlan, asr: Transcript | None) -> list[Word]:
    if asr is not None and asr.words:
        return asr.words
    return narration_word_times(plan)


def _check_captions(plan: EditPlan, asr: Transcript | None) -> CheckResult:
    if not plan.captions:
        return CheckResult("captions", True, "no captions on the plan", skipped=True)
    speech = _speech_words(plan, asr)
    findings: list[Finding] = []
    for cue in plan.captions:
        heard = words_overlapping(speech, cue.start - 0.15, cue.end + 0.15)
        if not heard:
            findings.append(
                _fail(
                    "captions",
                    "caption_without_speech",
                    "caption has no speech under it",
                    cue.start,
                )
            )
            continue
        speech_start = min(word.start for word in heard)
        speech_end = max(word.end for word in heard)
        if cue.start < speech_start - CAPTION_LEAD:
            findings.append(
                _fail(
                    "captions",
                    "caption_early",
                    f"caption starts {speech_start - cue.start:.2f}s before speech",
                    cue.start,
                )
            )
        if cue.end > speech_end + CAPTION_TRAIL:
            findings.append(
                _fail(
                    "captions",
                    "caption_late",
                    f"caption ends {cue.end - speech_end:.2f}s after speech",
                    cue.end,
                )
            )
        cue_tokens = {normalize_token(token) for token in tokenize(cue.text)}
        heard_tokens = {normalize_token(word.text) for word in heard}
        cue_tokens.discard("")
        heard_tokens.discard("")
        if cue_tokens and heard_tokens and cue_tokens.isdisjoint(heard_tokens):
            findings.append(
                _fail(
                    "captions",
                    "caption_mismatch",
                    "caption text does not match the speech",
                    cue.start,
                )
            )
    summary = f"{len(plan.captions)} cue(s)"
    return CheckResult("captions", not findings, summary, findings)


def _check_splices(video, plan, rules, transcriber, asr, window: float | None) -> CheckResult:
    span = float(window if window is not None else rules.lint.get("splice_window_seconds", 0.5))
    if not plan.narration and not plan.segments:
        return CheckResult("splice_fragments", True, "no splices", skipped=True)
    if not has_audio(video):
        return CheckResult(
            "splice_fragments",
            False,
            "video has no audio to transcribe",
            [_fail("splice_fragments", "no_audio", "video has no audio stream")],
        )
    splices = splice_times(plan)
    expected = narration_word_times(plan)
    findings: list[Finding] = []
    try:
        with tempfile.TemporaryDirectory(prefix="ytp-splice-") as work_text:
            work = Path(work_text)
            for splice in splices:
                start = max(0.0, splice - span)
                end = splice + span
                if asr is not None and transcriber is None:
                    heard = words_overlapping(asr.words, start, end)
                    transcript = Transcript(text=" ".join(word.text for word in heard), words=heard)
                else:
                    if transcriber is None:
                        return CheckResult(
                            "splice_fragments",
                            False,
                            "no transcript. Install faster-whisper or pass --asr.",
                            [
                                _fail(
                                    "splice_fragments",
                                    "transcript_missing",
                                    "refusing to pass a splice check with no transcript",
                                    splice,
                                )
                            ],
                        )
                    clip = work / f"splice-{start:.3f}.wav"
                    extract_audio(video, clip, start=start, end=end)
                    try:
                        local = transcriber.transcribe(clip)
                    except RuntimeError as exc:
                        if "faster-whisper" in str(exc):
                            return CheckResult(
                                "splice_fragments",
                                False,
                                str(exc),
                                [_fail("splice_fragments", "transcript_missing", str(exc), splice)],
                            )
                        raise
                    transcript = local.shifted(start)
                approved = words_overlapping(expected, start, end)
                for code, word in compare_window(approved, transcript.words):
                    label = word.text.strip()
                    findings.append(
                        _fail(
                            "splice_fragments",
                            code,
                            (
                                f'{code.replace("_", " ")} "{label}" at splice '
                                f"{format_timestamp(splice)} is not in the approved script"
                            ),
                            word.start,
                            label,
                        )
                    )
    except Exception as exc:
        return CheckResult(
            "splice_fragments",
            False,
            f"splice check failed: {exc}",
            [_fail("splice_fragments", "error", str(exc))],
        )
    findings = _dedupe(findings)
    summary = f"{len(splices)} splice window(s), +/- {span:.2f}s"
    if findings:
        summary = f"{summary}; {len(findings)} leftover fragment(s)"
    return CheckResult("splice_fragments", not findings, summary, findings)
