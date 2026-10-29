"""Pass/fail report for a rendered episode.

Checks caption timing, integrated loudness and true peak, words per minute,
banned words and sound-effect ids from the series rules, and the splice
windows. The whole file is aligned to the script, and each narration join
is listened to again on a short clip. A leftover fragment fails the cut.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ytp.captions import format_timestamp
from ytp.cards import card_box
from ytp.ff import has_audio, probe_duration, probe_video_size, run_ffmpeg
from ytp.plan import EditPlan
from ytp.rules import SeriesRules, collected_bans, load_rules
from ytp.splice import (
    fragments_at_join,
    narration_join_edges,
    normalize_token,
    stray_near_joins,
    tokenize,
    window_is_hallucination,
    words_overlapping,
)
from ytp.transcribe import Transcript, Word

CAPTION_LEAD = 0.40
CAPTION_TRAIL = 0.50
DURATION_TOLERANCE = 0.75
# Audio re-heard around each narration join. The near-join margin stays
# splice_window_seconds; this is only how much of the cut Whisper hears.
JOIN_LISTEN_SECONDS = 1.5
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
            status = _check_status(check)
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
    """Overall pace: script words over the narration span, pauses included."""
    words = sum(len(tokenize(line.text)) for line in plan.narration)
    seconds = narration_span(plan)
    if words == 0 or seconds is None:
        return None
    return words / (seconds / 60.0)


def narration_span(plan: EditPlan) -> float | None:
    """First narration start through the last end. Gaps between lines count."""
    if not plan.narration:
        return None
    span = max(line.end for line in plan.narration) - min(line.start for line in plan.narration)
    if span <= 0:
        return None
    return span


def speech_seconds(words: list[Word], gap_seconds: float) -> float:
    """Word time plus gaps at or under the pause threshold. Longer gaps drop out."""
    ordered = sorted(words, key=lambda word: (word.start, word.end))
    if not ordered:
        return 0.0
    total = 0.0
    previous_end: float | None = None
    for word in ordered:
        if previous_end is not None:
            gap = word.start - previous_end
            if 0 <= gap <= gap_seconds:
                total += gap
        total += max(0.0, word.end - word.start)
        previous_end = word.end if previous_end is None else max(previous_end, word.end)
    return total


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
        words = []
        for item in rows:
            confidence = item.get("confidence")
            if confidence is None:
                confidence = item.get("probability")
            words.append(
                Word(
                    str(item.get("word") or item.get("text") or ""),
                    float(item["start"]),
                    float(item["end"]),
                    None if confidence is None else float(confidence),
                )
            )
        return Transcript(
            text=" ".join(word.text for word in words),
            words=words,
            word_timestamps=True,
        )
    pieces: list[str] = []
    segments: list[tuple[float, float, str]] = []
    pattern = re.compile(r"^\[\s*([0-9.]+)\s*-\s*([0-9.]+)\s*\]\s*(.*)$")
    for line in raw.splitlines():
        match = pattern.match(line.strip())
        if not match:
            continue
        start = float(match.group(1))
        end = float(match.group(2))
        sentence = match.group(3).strip()
        pieces.append(sentence)
        segments.append((start, end, sentence))
    return Transcript(
        text=" ".join(pieces),
        words=[],
        word_timestamps=False,
        segments=segments,
    )


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
    transcript = _measured_transcript(video, asr, transcriber)
    width, height = probe_video_size(video)
    duration = probe_duration(video)
    use_short = profile == "short" or (profile == "auto" and height > width)
    checks = [
        _check_container(video, plan, width, height, duration, use_short),
        _check_loudness(video, plan, rules, use_short, loudness_fn, episode_dir),
        _check_wpm(plan, rules, transcript),
        _check_banned(plan, rules, transcript),
        _check_closers(plan, rules, transcript),
        _check_required_open(plan, rules, transcript),
        _check_description(plan, rules, episode_dir),
        _check_end_card(plan, rules, episode_dir, duration),
        _check_cards(video, plan, rules),
        _check_captions(plan, transcript),
        _check_splices(video, plan, rules, transcriber, transcript, window),
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
    """Episode-relative paths only. A relative path is never resolved from the CWD."""
    if not value or episode_dir is None:
        return None
    candidate = resolve_episode_path(episode_dir, value)
    if _DRIVE.match(str(value)) or Path(str(value)).is_absolute():
        return candidate if candidate.is_file() else None
    return candidate if candidate.is_file() else None


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
        measurements["music_under_voice"] = "not measured"
        named = [cue.source for cue in plan.music if cue.source]
        if named and not cues:
            return (
                "not measured: score files are not on disk "
                f"({len(named)} cue path(s) under audio/score/). "
                "A finished mix cannot be split into voice and music, so the "
                "music-under-voice check did not run."
            )
        return (
            "not measured: no separate voice stem and score files. "
            "The music-under-voice check did not run."
        )
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


def _check_status(check: CheckResult) -> str:
    if check.skipped:
        return "SKIP"
    if any(item.severity == "fail" for item in check.findings):
        return "FAIL"
    if any(item.severity == "warn" for item in check.findings):
        return "WARN"
    return "PASS"


def _ok(findings: list[Finding]) -> bool:
    return not any(item.severity == "fail" for item in findings)


def _warn(
    check: str,
    code: str,
    message: str,
    timestamp: float | None = None,
    word: str | None = None,
) -> Finding:
    return Finding(check, "warn", code, message, timestamp, word)


def _pace_source(plan: EditPlan, rules: SeriesRules) -> str:
    chosen = str((plan.meta or {}).get("source_type") or "")
    if chosen in {"narration", "desk"}:
        return chosen
    configured = str((rules.lint.get("wpm") or {}).get("source_type") or "narration")
    return configured if configured in {"narration", "desk"} else "narration"


def _pace_band(rules: SeriesRules, source: str, metric: str) -> tuple[float, float, str]:
    wpm = rules.lint.get("wpm") or {}
    block = (wpm.get(source) or {}).get(metric) or {}
    low = float(block.get("min", 155 if metric == "overall" else 175))
    high = float(block.get("max", 185))
    severity = str(block.get("severity") or "warn").lower()
    if severity not in {"warn", "fail"}:
        severity = "warn"
    if source == "desk":
        severity = "warn"
    return low, high, severity


def _measured_transcript(video: Path, asr: Transcript | None, transcriber) -> Transcript | None:
    """Word-timed transcript of the video. Whisper fills this in when --asr is absent."""
    if asr is not None and asr.word_timestamps and asr.words:
        return asr
    if transcriber is None:
        return asr
    try:
        heard = transcriber.transcribe(video)
    except RuntimeError as exc:
        if "faster-whisper" in str(exc):
            return asr
        raise
    if heard is None:
        return asr
    heard.word_timestamps = True
    if asr is not None and asr.text and not heard.text:
        heard.text = asr.text
    return heard


def _editorial_pause(plan: EditPlan, start: float, end: float, gap_seconds: float) -> float:
    """Silence between narration lines, when that silence is longer than the threshold.

    Time inside a line stays in the speech clock even if Whisper left a hole
    there. Only the gap the edit actually left between lines is a pause.
    """
    pause = 0.0
    lines = sorted(plan.narration, key=lambda line: (line.start, line.end))
    for previous, following in zip(lines, lines[1:]):
        gap = following.start - previous.end
        if gap <= gap_seconds:
            continue
        left = max(previous.end, start)
        right = min(following.start, end)
        if right > left:
            pause += right - left
    return pause


def _pace_from_transcript(
    transcript: Transcript | None,
    plan: EditPlan,
    gap_seconds: float,
) -> tuple[float | None, float | None, str]:
    """Overall and articulation pace from the video transcript, not the plan text.

    Articulation removes pauses, so it cannot be slower than overall. A
    bracket transcript has no word times: overall can use the line span, and
    articulation is not measured.
    """
    if transcript is None:
        return None, None, "not measured: no transcript of the video"
    if transcript.word_timestamps and transcript.words:
        spoken = sorted(
            (word for word in transcript.words if normalize_token(word.text)),
            key=lambda word: (word.start, word.end),
        )
        if len(spoken) < 1:
            return None, None, "not measured: transcript has no words"
        span = spoken[-1].end - spoken[0].start
        if span <= 0:
            return None, None, "not measured: transcript words have no duration"
        overall = len(spoken) / (span / 60.0)
        # Only editorial pauses (gaps between narration lines) come out.
        # Whisper's holes between words inside a line are not pauses.
        pause = _editorial_pause(plan, spoken[0].start, spoken[-1].end, gap_seconds)
        speech = max(span - pause, 1e-6)
        articulation = len(spoken) / (speech / 60.0)
        if articulation is not None and articulation + 0.05 < overall:
            return overall, None, "articulation not measured: it would be below overall pace"
        return overall, articulation, ""
    if transcript.segments:
        count = sum(len(tokenize(text)) for _start, _end, text in transcript.segments)
        span = transcript.segments[-1][1] - transcript.segments[0][0]
        if count and span > 0:
            return (
                count / (span / 60.0),
                None,
                "articulation not measured: transcript has no word timestamps",
            )
    return None, None, "not measured: transcript has no word timestamps"


def _check_wpm(plan: EditPlan, rules: SeriesRules, asr: Transcript | None) -> CheckResult:
    source = _pace_source(plan, rules)
    gap_ms = float((rules.lint.get("wpm") or {}).get("pause_gap_ms", 250))
    pace, articulation, note = _pace_from_transcript(asr, plan, gap_ms / 1000.0)
    if pace is None:
        return CheckResult("wpm", True, note or "not measured", skipped=True)
    findings: list[Finding] = []
    measurements: dict[str, object] = {
        "source_type": source,
        "pause_gap_ms": gap_ms,
        "overall_wpm": round(pace, 1),
    }
    low, high, severity = _pace_band(rules, source, "overall")
    if pace < low or pace > high:
        message = (
            f"overall pace is {pace:.1f} wpm; {source} band {low:.0f}-{high:.0f} ({severity})"
        )
        marker = _fail if severity == "fail" else _warn
        findings.append(marker("wpm", "overall_wpm", message))
    if articulation is None:
        summary = f"overall {pace:.1f} wpm; {note or 'articulation not measured'}"
    else:
        measurements["articulation_wpm"] = round(articulation, 1)
        band_low, band_high, band_severity = _pace_band(rules, source, "articulation")
        if articulation < band_low or articulation > band_high:
            message = (
                f"articulation pace is {articulation:.1f} wpm; "
                f"{source} band {band_low:.0f}-{band_high:.0f} ({band_severity})"
            )
            marker = _fail if band_severity == "fail" else _warn
            findings.append(marker("wpm", "articulation_wpm", message))
        summary = f"overall {pace:.1f} wpm; articulation {articulation:.1f} wpm"
    return CheckResult("wpm", _ok(findings), summary, findings, measurements=measurements)


_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


def resolve_episode_path(episode_dir: Path | None, value: str) -> Path:
    """Episode-relative path, or an absolute path including a Windows drive."""
    if _DRIVE.match(value) or Path(value).is_absolute():
        return Path(value)
    relative = Path(value.replace("\\", "/"))
    if episode_dir is None:
        return relative
    return episode_dir / relative


def _copy_text(*parts: str) -> str:
    return " ".join(part for part in parts if part).replace("\u2019", "'").replace("\u2018", "'")


def _check_closers(plan: EditPlan, rules: SeriesRules, asr: Transcript | None) -> CheckResult:
    phrases = [str(item) for item in (rules.lint.get("closers") or [])]
    if not phrases:
        return CheckResult("closers", True, "no closer list", skipped=True)
    findings: list[Finding] = []
    blobs: list[tuple[str, float | None]] = [
        (line.text, line.start) for line in plan.narration
    ]
    blobs.extend((cue.text, cue.start) for cue in plan.captions)
    for segment in plan.segments:
        if segment.template_text:
            blobs.append((segment.template_text, segment.start))
    if asr is not None and asr.text:
        blobs.append((asr.text, asr.words[0].start if asr.words else None))
    for text, stamp in blobs:
        for phrase in phrases:
            if _contains(text, phrase):
                findings.append(
                    _fail(
                        "closers",
                        "closer",
                        f'closer "{phrase}" is not allowed',
                        stamp,
                        phrase,
                    )
                )
    findings = _dedupe(findings)
    summary = "no closers" if not findings else f"{len(findings)} closer(s)"
    return CheckResult("closers", _ok(findings), summary, findings)


def _check_required_open(plan: EditPlan, rules: SeriesRules, asr: Transcript | None) -> CheckResult:
    block = rules.lint.get("required_open") or {}
    required = str(block.get("text") or "").strip()
    if not required:
        return CheckResult("required_open", True, "no required open line", skipped=True)
    window = float(block.get("window_seconds", 60))
    if asr is not None and asr.words:
        heard = " ".join(word.text for word in asr.words if word.start < window)
        parts = [heard]
    else:
        parts = [line.text for line in plan.narration if line.start < window]
    haystack = _copy_text(*parts).casefold()
    needle = _copy_text(required).casefold()
    if needle in haystack:
        return CheckResult("required_open", True, f"open line is inside the first {window:.0f}s")
    return CheckResult(
        "required_open",
        False,
        f'missing "{required}" in the first {window:.0f}s',
        [
            _fail(
                "required_open",
                "missing_open",
                f'missing "{required}" in the first {window:.0f}s',
            )
        ],
    )


def _description_text(plan: EditPlan, episode_dir: Path | None) -> str:
    meta = str((plan.meta or {}).get("description") or "")
    if meta.strip():
        return meta
    if episode_dir is None:
        return ""
    path = episode_dir / "text" / "description.txt"
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return ""


def _link_present(text: str, needle: str) -> bool:
    lowered = text.casefold()
    if needle.casefold() in lowered:
        return True
    if needle.casefold() in {"x.com", "twitter.com"}:
        return "x.com" in lowered or "twitter.com" in lowered
    return False


def _check_description(plan: EditPlan, rules: SeriesRules, episode_dir: Path | None) -> CheckResult:
    block = rules.lint.get("description") or {}
    required = [str(item) for item in (block.get("require") or [])]
    if not required:
        return CheckResult("description", True, "no description links required", skipped=True)
    text = _description_text(plan, episode_dir)
    findings = [
        _fail("description", "missing_link", f'description does not link to "{needle}"')
        for needle in required
        if not _link_present(text, needle)
    ]
    summary = "description links are present" if not findings else "description is missing a link"
    return CheckResult("description", _ok(findings), summary, findings)


def _slots(
    rules: SeriesRules, width: int, height: int
) -> list[tuple[str, tuple[float, float, float, float]]]:
    block = rules.lint.get("end_card") or {}
    ref_w = float(block.get("reference_width") or 1920)
    ref_h = float(block.get("reference_height") or 1080)
    scale_x = width / ref_w if ref_w else 1
    scale_y = height / ref_h if ref_h else 1
    scaled = []
    for slot in block.get("clear_slots") or []:
        box = (
            float(slot["x1"]) * scale_x,
            float(slot["y1"]) * scale_y,
            float(slot["x2"]) * scale_x,
            float(slot["y2"]) * scale_y,
        )
        scaled.append((str(slot.get("id") or "slot"), box))
    return scaled


def _overlaps(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> bool:
    return left[0] < right[2] and left[2] > right[0] and left[1] < right[3] and left[3] > right[1]


def _check_end_card(
    plan: EditPlan,
    rules: SeriesRules,
    episode_dir: Path | None,
    video_duration: float | None = None,
) -> CheckResult:
    block = rules.lint.get("end_card") or {}
    if not block:
        return CheckResult("end_card", True, "no end card rule", skipped=True)
    if not plan.narration:
        return CheckResult(
            "end_card",
            False,
            "no narration, so the end card has no last line to follow",
            [_fail("end_card", "no_narration", "end card needs a last spoken line")],
        )
    last = max(line.end for line in plan.narration)
    end = plan.picture_duration()
    if video_duration is not None:
        end = max(end, video_duration)
    tail = end - last
    low = float(block.get("min_seconds", 5))
    high = float(block.get("max_seconds", 10))
    findings: list[Finding] = []
    if tail < low or tail > high:
        findings.append(
            _fail(
                "end_card",
                "end_card_duration",
                f"end card runs {tail:.2f}s after the last line; expected {low:.0f}-{high:.0f}s",
                last,
            )
        )
    image = str((plan.meta or {}).get("end_card", {}).get("image") or block.get("image") or "")
    if image:
        resolved = resolve_episode_path(episode_dir, image)
        if episode_dir is not None and not resolved.is_file():
            findings.append(
                _warn(
                    "end_card",
                    "end_card_image",
                    f"end card image is not on disk: {resolved}",
                    last,
                )
            )
    elements = list(((plan.meta or {}).get("end_card") or {}).get("elements") or [])
    for name, slot in _slots(rules, plan.width, plan.height):
        for element in elements:
            box = (
                float(element["x1"]),
                float(element["y1"]),
                float(element["x2"]),
                float(element["y2"]),
            )
            if _overlaps(box, slot):
                label = str(element.get("id") or "element")
                findings.append(
                    _fail(
                        "end_card",
                        "end_screen_overlap",
                        f"{label} overlaps the {name} end-screen slot",
                        last,
                        label,
                    )
                )
    if low <= tail <= high and not any(item.severity == "fail" for item in findings):
        summary = f"end card {tail:.2f}s, end-screen slots clear"
    else:
        summary = f"end card {tail:.2f}s after the last line"
    return CheckResult(
        "end_card",
        _ok(findings),
        summary,
        findings,
        measurements={"tail_seconds": round(tail, 3)},
    )


def _load_card_manifest(video: Path) -> list[dict] | None:
    path = video.with_name("cards.json")
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    cards = data.get("cards") if isinstance(data, dict) else None
    if not isinstance(cards, list):
        return None
    return cards


def _check_cards(video: Path, plan: EditPlan, rules: SeriesRules) -> CheckResult:
    """Card text matches the plan, and no card sits on an end-screen slot."""
    manifest = _load_card_manifest(video)
    if not plan.cards and not manifest:
        return CheckResult("cards", True, "no cards", skipped=True)
    findings: list[Finding] = []
    boxes: list[tuple[str, tuple[float, float, float, float]]] = []
    if manifest is None:
        for card in plan.cards:
            boxes.append((card.id, card_box(card.style, plan.width, plan.height)))
    else:
        by_id = {str(item.get("id")): item for item in manifest}
        for card in plan.cards:
            got = by_id.get(card.id)
            heard = "" if got is None else str(got.get("text") or "")
            if got is None or heard != card.text:
                findings.append(
                    _fail(
                        "cards",
                        "card_text",
                        f"card {card.id} text does not match the plan",
                        card.start,
                        card.id,
                    )
                )
        for item in manifest:
            box = (
                float(item.get("x1") or 0),
                float(item.get("y1") or 0),
                float(item.get("x2") or 0),
                float(item.get("y2") or 0),
            )
            boxes.append((str(item.get("id") or "card"), box))
    for name, slot in _slots(rules, plan.width, plan.height):
        for label, box in boxes:
            if _overlaps(box, slot):
                findings.append(
                    _fail(
                        "cards",
                        "end_screen_overlap",
                        f"{label} overlaps the {name} end-screen slot",
                        None,
                        label,
                    )
                )
    summary = f"{len(plan.cards)} card(s)" if not findings else "card check failed"
    return CheckResult("cards", not findings, summary, findings)


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
    if asr is not None and asr.words:
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
    elif asr is not None and asr.text:
        for phrase in phrases:
            if _contains(asr.text, phrase):
                findings.append(
                    _fail(
                        "banned",
                        "banned_phrase",
                        f'narration audio contains "{phrase}"',
                        None,
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


def _check_captions(plan: EditPlan, asr: Transcript | None) -> CheckResult:
    if not plan.captions:
        return CheckResult("captions", True, "no captions on the plan", skipped=True)
    if asr is None or not asr.word_timestamps or not asr.words:
        return CheckResult(
            "captions",
            True,
            "not measured: caption timing needs a word-timestamp transcript of the video",
            [_warn("captions", "not_measured", "caption timing was not compared to the audio")],
        )
    speech = asr.words
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


def _one_join_finding(code: str, words: list[Word], join: float) -> Finding:
    """One finding per cut. The message lists the words heard at that cut."""
    labels: list[str] = []
    for word in sorted(words, key=lambda item: (item.start, item.end)):
        text = word.text.strip()
        if text and text not in labels:
            labels.append(text)
    label = " ".join(labels)
    return _fail(
        "splice_fragments",
        code,
        (
            f'{code.replace("_", " ")} "{label}" near a narration join '
            "is not in the approved script"
        ),
        words[0].start if words else join,
        label,
    )


def _window_fragments(video, plan, transcriber, near: float) -> dict[float, list[tuple[str, Word]]]:
    """Re-hear ±1.5s around each join of `video`. The plan's voice file is not opened."""
    grouped: dict[float, list[tuple[str, Word]]] = {}
    for join, before, after in narration_join_edges(plan):
        start = max(0.0, join - JOIN_LISTEN_SECONDS)
        end = join + JOIN_LISTEN_SECONDS
        heard = transcriber.transcribe(video, start=start, end=end)
        if window_is_hallucination(heard.words):
            continue
        flagged = fragments_at_join(
            before,
            after,
            heard.words,
            join,
            near=near,
            edge_seconds=JOIN_LISTEN_SECONDS,
        )
        if flagged:
            grouped.setdefault(join, []).extend(flagged)
    return grouped


def _findings_for_joins(grouped: dict[float, list[tuple[str, Word]]]) -> list[Finding]:
    findings: list[Finding] = []
    for join in sorted(grouped):
        items = grouped[join]
        partial = any(name == "partial_word" for name, _word in items)
        code = "partial_word" if partial else "stray_fragment"
        findings.append(_one_join_finding(code, [word for _name, word in items], join))
    return findings


def _check_splices(video, plan, rules, transcriber, asr, window: float | None) -> CheckResult:
    """Align the whole-file transcript, then listen again at each join.

    The voice path is not an input. Window clips are cut from the video
    being checked, so two plans that share text and timing agree.
    """
    span = float(window if window is not None else rules.lint.get("splice_window_seconds", 0.5))
    if not plan.narration:
        return CheckResult("splice_fragments", True, "no narration splices", skipped=True)
    whole_words = bool(asr is not None and asr.word_timestamps and asr.words)
    if not whole_words and transcriber is None:
        note = (
            "not measured: the splice check needs one word-timestamp transcript "
            "of the audio being checked"
        )
        return CheckResult(
            "splice_fragments",
            True,
            note,
            [_warn("splice_fragments", "not_measured", note)],
        )
    if transcriber is not None and not has_audio(video):
        return CheckResult(
            "splice_fragments",
            False,
            "video has no audio to transcribe",
            [_fail("splice_fragments", "no_audio", "video has no audio stream")],
        )
    grouped: dict[float, list[tuple[str, Word]]] = {}
    if whole_words:
        cuts = [time for time, _before, _after in narration_join_edges(plan)]
        for code, word in stray_near_joins(plan, asr.words, margin=span):
            if not cuts:
                continue
            join = min(cuts, key=lambda time: abs(word.start - time))
            if abs(word.start - join) <= span:
                grouped.setdefault(join, []).append((code, word))
    if transcriber is not None:
        try:
            for join, items in _window_fragments(video, plan, transcriber, span).items():
                grouped.setdefault(join, []).extend(items)
        except RuntimeError as exc:
            if "faster-whisper" in str(exc):
                return CheckResult(
                    "splice_fragments",
                    False,
                    str(exc),
                    [_fail("splice_fragments", "transcript_missing", str(exc))],
                )
            raise
    findings = _dedupe(_findings_for_joins(grouped))
    summary = (
        f"aligned to the script; each join is re-heard "
        f"±{JOIN_LISTEN_SECONDS:.2f}s, margin {span:.2f}s"
    )
    if findings:
        summary = f"{summary}; {len(findings)} leftover fragment(s)"
    return CheckResult("splice_fragments", not findings, summary, findings)
