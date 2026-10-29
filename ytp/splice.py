"""Compare narration audio to the approved script.

Picture cuts are not audio splices. The whole-file transcript is aligned to
the plan text. Each narration join is also listened to on its own short
clip, because the full pass can skip a fragment sitting in the gap. A word
from that clip is a fragment only when it is near the join and matches
neither the last words of the line before nor the first words of the line
after. Digits are spelled out first. Fuzzy and phonetic neighbors match.
Words under the confidence floor are ignored.

Episode 1 v8 left "It's", the start of removed line 00d_b1, at about 17.5s.
The whole-file pass hears "target." then "Your" and skips it. The scripted
"if" in "And if one comes down" is not that bug.
"""

from __future__ import annotations

import math
import re
from difflib import SequenceMatcher

from ytp.plan import EditPlan, NarrationLine
from ytp.transcribe import Word

_TOKEN = re.compile(r"[A-Za-z0-9']+")


def normalize_token(text: str) -> str:
    cleaned = text.replace("\u2019", "'").replace("\u2018", "'").casefold()
    cleaned = re.sub(r"[^a-z0-9']+", "", cleaned)
    return cleaned.strip("'")


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.replace("\u2019", "'").replace("\u2018", "'"))


def narration_word_times(plan: EditPlan) -> list[Word]:
    """Spread each line's words evenly across its start and end.

    The plan has line times, not word times. This is only a stand-in clock.
    The splice check aligns text; it does not treat these times as the words
    the cut was allowed to say.
    """
    timed: list[Word] = []
    for line in plan.narration:
        tokens = tokenize(line.text)
        if not tokens:
            continue
        span = max(line.end - line.start, 1e-3)
        step = span / len(tokens)
        for index, token in enumerate(tokens):
            start = line.start + step * index
            timed.append(Word(token, start, start + step))
    return timed


def splice_times(plan: EditPlan, *, gap: float = 0.3) -> list[float]:
    """Narration edit points only. A still change in the middle of a line is not a splice."""
    times: list[float] = []
    for line in plan.narration:
        times.append(line.start)
        times.append(line.end)
    return _cluster([time for time in times if time >= 0], gap)


def audio_splice_windows(
    plan: EditPlan,
    *,
    margin: float = 0.5,
    split_gap: float = 1.5,
) -> list[tuple[float, float]]:
    """Windows that cover each narration join, including the gap between lines.

    A long designed pause is checked at each edge instead of as one span.
    """
    lines = sorted(plan.narration, key=lambda line: (line.start, line.end))
    if not lines:
        return []
    windows: list[tuple[float, float]] = []
    windows.append((max(0.0, lines[0].start - margin), lines[0].start + margin))
    for previous, following in zip(lines, lines[1:]):
        gap = following.start - previous.end
        if gap <= split_gap:
            windows.append((max(0.0, previous.end - margin), following.start + margin))
        else:
            windows.append((max(0.0, previous.end - margin), previous.end + margin))
            windows.append((max(0.0, following.start - margin), following.start + margin))
    last = lines[-1]
    windows.append((max(0.0, last.end - margin), last.end + margin))
    return _merge_windows(windows)


def _cluster(times: list[float], gap: float) -> list[float]:
    if not times:
        return []
    ordered = sorted(times)
    groups: list[list[float]] = [[ordered[0]]]
    for time in ordered[1:]:
        if time - groups[-1][-1] <= gap:
            groups[-1].append(time)
        else:
            groups.append([time])
    return [sum(group) / len(group) for group in groups]


def _merge_windows(windows: list[tuple[float, float]]) -> list[tuple[float, float]]:
    ordered = sorted(windows)
    merged: list[tuple[float, float]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def words_overlapping(words: list[Word], start: float, end: float) -> list[Word]:
    return [word for word in words if word.end > start and word.start < end]


# Whisper words under this probability are treated as mishearings, not fragments.
CONFIDENCE_MIN = 0.55
_SOUNDEX_GROUPS = {
    "B": "1",
    "F": "1",
    "P": "1",
    "V": "1",
    "C": "2",
    "G": "2",
    "J": "2",
    "K": "2",
    "Q": "2",
    "S": "2",
    "X": "2",
    "Z": "2",
    "D": "3",
    "T": "3",
    "L": "4",
    "M": "5",
    "N": "5",
    "R": "6",
}


_SMALL = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
]
_TENS_WORDS = [
    "",
    "",
    "twenty",
    "thirty",
    "forty",
    "fifty",
    "sixty",
    "seventy",
    "eighty",
    "ninety",
]


def spell_number(value: int) -> list[str]:
    """Spell a digit run so '7' and 'seven' compare as the same tokens."""
    if value < 0:
        return ["minus", *spell_number(-value)]
    if value < 20:
        return [_SMALL[value]]
    if value < 100:
        tens, ones = divmod(value, 10)
        if ones == 0:
            return [_TENS_WORDS[tens]]
        return [_TENS_WORDS[tens], _SMALL[ones]]
    if value < 1000:
        hundreds, rest = divmod(value, 100)
        head = [_SMALL[hundreds], "hundred"]
        return head if rest == 0 else [*head, *spell_number(rest)]
    return [str(value)]


def canonicalize_tokens(tokens: list[str]) -> list[str]:
    """Spell digits out and keep number words as words."""
    folded: list[str] = []
    for raw in tokens:
        token = normalize_token(raw)
        if not token:
            continue
        if token.isdigit():
            folded.extend(spell_number(int(token)))
            continue
        folded.append(token)
    return folded


def _soundex(token: str) -> str:
    if not token:
        return ""
    upper = token.upper()
    first = upper[0]
    digits: list[str] = []
    previous = _SOUNDEX_GROUPS.get(first, "")
    for char in upper[1:]:
        digit = _SOUNDEX_GROUPS.get(char, "")
        if not digit:
            previous = ""
            continue
        if digit != previous:
            digits.append(digit)
        previous = digit
    return (first + "".join(digits) + "000")[:4]


def _tokens_close(left: str, right: str) -> bool:
    """True when two tokens are the same word, or a fuzzy/phonetic neighbor."""
    if left == right:
        return True
    if len(left) >= 3 and len(right) >= 3:
        # 0.66 pairs a one-slot mishearing such as "more" and "score".
        # "is" and "it" are shorter than 3 characters, so they do not absorb "It's".
        if SequenceMatcher(a=left, b=right, autojunk=False).ratio() >= 0.66:
            return True
        if _soundex(left) == _soundex(right) and not _soundex(left).endswith("000"):
            return True
        shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
        if len(shorter) >= 3 and longer.startswith(shorter[:3]):
            if min(len(left), len(right)) / max(len(left), len(right)) >= 0.5:
                return True
    if len(right) >= 4 and left.startswith(right):
        return True
    if len(left) >= 4 and right.startswith(left):
        return True
    return False


def _is_clipped_prefix(token: str, expected: list[str]) -> bool:
    if len(token) < 2:
        return False
    return any(exp.startswith(token) and token != exp and len(token) < 4 for exp in expected)


def _heard_tokens(words: list[Word]) -> list[tuple[str, Word]]:
    heard: list[tuple[str, Word]] = []
    for word in words:
        if word.confidence is not None and word.confidence < CONFIDENCE_MIN:
            continue
        for token in canonicalize_tokens(tokenize(word.text)):
            heard.append((token, word))
    return heard


def unmatched_asr(plan_tokens: list[str], heard: list[tuple[str, Word]]) -> list[tuple[str, Word]]:
    """ASR tokens left over after aligning them to the plan text, in order.

    A fuzzy neighbor counts as the script word. Anything else that alignment
    cannot call that word is leftover: an extra token, or a different word
    standing in a script slot.
    """
    plan_n = len(plan_tokens)
    heard_n = len(heard)
    insert_cost = 1
    delete_cost = 1
    substitute_cost = 4
    parent = [[0] * (heard_n + 1) for _ in range(plan_n + 1)]
    previous = [index * insert_cost for index in range(heard_n + 1)]
    for index in range(1, heard_n + 1):
        parent[0][index] = 1
    for plan_index in range(1, plan_n + 1):
        current = [0] * (heard_n + 1)
        current[0] = plan_index * delete_cost
        parent[plan_index][0] = 2
        plan_token = plan_tokens[plan_index - 1]
        for heard_index in range(1, heard_n + 1):
            heard_token = heard[heard_index - 1][0]
            match_cost = 0 if _tokens_close(heard_token, plan_token) else substitute_cost
            choices = (
                (previous[heard_index - 1] + match_cost, 0),
                (current[heard_index - 1] + insert_cost, 1),
                (previous[heard_index] + delete_cost, 2),
            )
            cost, move = min(choices)
            current[heard_index] = cost
            parent[plan_index][heard_index] = move
        previous = current
    plan_index, heard_index = plan_n, heard_n
    unmatched: list[tuple[str, Word]] = []
    while plan_index > 0 or heard_index > 0:
        move = parent[plan_index][heard_index]
        if plan_index > 0 and heard_index > 0 and move == 0:
            if not _tokens_close(heard[heard_index - 1][0], plan_tokens[plan_index - 1]):
                unmatched.append(heard[heard_index - 1])
            plan_index -= 1
            heard_index -= 1
        elif heard_index > 0 and (plan_index == 0 or move == 1):
            unmatched.append(heard[heard_index - 1])
            heard_index -= 1
        else:
            plan_index -= 1
    unmatched.reverse()
    return unmatched


def _classify(token: str, plan_tokens: list[str]) -> str:
    if _is_clipped_prefix(token, plan_tokens):
        return "partial_word"
    return "stray_fragment"


def compare_window(expected: list[Word], actual: list[Word]) -> list[tuple[str, Word]]:
    """Unmatched heard words after aligning them to the expected words."""
    raw: list[str] = []
    for word in expected:
        raw.extend(tokenize(word.text))
    plan_tokens = canonicalize_tokens(raw)
    seen: set[int] = set()
    extras: list[tuple[str, Word]] = []
    for token, word in unmatched_asr(plan_tokens, _heard_tokens(actual)):
        if id(word) in seen:
            continue
        seen.add(id(word))
        extras.append((_classify(token, plan_tokens), word))
    return extras


def narration_joins(plan: EditPlan) -> list[float]:
    lines = sorted(plan.narration, key=lambda line: (line.start, line.end))
    if not lines:
        return []
    points = [lines[0].start]
    for previous, following in zip(lines, lines[1:]):
        points.append(previous.end)
        points.append(following.start)
    points.append(lines[-1].end)
    return points


def plan_tokens(plan: EditPlan) -> list[str]:
    raw: list[str] = []
    for line in sorted(plan.narration, key=lambda item: (item.start, item.end)):
        raw.extend(tokenize(line.text))
    return canonicalize_tokens(raw)


def edge_tokens(line: NarrationLine, side: str, *, seconds: float) -> list[str]:
    """First or last words of a line, enough to cover `seconds` of that line.

    The count follows the line's own pace and is at least eight words when
    the line has them, so a fast opening still counts as the line's edge.
    """
    tokens = canonicalize_tokens(tokenize(line.text))
    if not tokens:
        return []
    duration = max(line.end - line.start, 1e-3)
    span = min(seconds, duration)
    by_pace = math.ceil(len(tokens) * span / duration)
    count = min(len(tokens), max(by_pace, 8))
    if side == "last":
        return tokens[-count:]
    return tokens[:count]


def fragments_at_join(
    before: NarrationLine | None,
    after: NarrationLine | None,
    heard: list[Word],
    join: float,
    *,
    near: float,
    edge_seconds: float,
) -> list[tuple[str, Word]]:
    """Heard words near `join` that are not the edge of either adjacent line."""
    expected: list[str] = []
    if before is not None:
        expected.extend(edge_tokens(before, "last", seconds=edge_seconds))
    if after is not None:
        expected.extend(edge_tokens(after, "first", seconds=edge_seconds))
    findings: list[tuple[str, Word]] = []
    seen: set[int] = set()
    for word in heard:
        if id(word) in seen:
            continue
        if word.confidence is not None and word.confidence < CONFIDENCE_MIN:
            continue
        tokens = canonicalize_tokens(tokenize(word.text))
        if not tokens:
            continue
        midpoint = (word.start + word.end) / 2
        if abs(midpoint - join) > near:
            continue
        if any(_tokens_close(token, exp) for token in tokens for exp in expected):
            seen.add(id(word))
            continue
        seen.add(id(word))
        findings.append((_classify(tokens[0], expected), word))
    return findings


def narration_join_edges(
    plan: EditPlan,
) -> list[tuple[float, NarrationLine | None, NarrationLine | None]]:
    """Each narration boundary and the lines that meet there."""
    lines = sorted(plan.narration, key=lambda line: (line.start, line.end))
    if not lines:
        return []
    edges: list[tuple[float, NarrationLine | None, NarrationLine | None]] = [
        (lines[0].start, None, lines[0])
    ]
    for previous, following in zip(lines, lines[1:]):
        edges.append((previous.end, previous, following))
        if following.start - previous.end > 0.05:
            edges.append((following.start, previous, following))
    edges.append((lines[-1].end, lines[-1], None))
    return edges


def stray_near_joins(
    plan: EditPlan,
    heard_words: list[Word],
    *,
    margin: float,
) -> list[tuple[str, Word]]:
    """ASR words near a narration join that alignment could not place in the script."""
    joins = narration_joins(plan)
    tokens = plan_tokens(plan)
    seen: set[int] = set()
    findings: list[tuple[str, Word]] = []
    for token, word in unmatched_asr(tokens, _heard_tokens(heard_words)):
        if id(word) in seen:
            continue
        midpoint = (word.start + word.end) / 2
        if not any(abs(midpoint - join) <= margin for join in joins):
            continue
        seen.add(id(word))
        findings.append((_classify(token, tokens), word))
    return findings
