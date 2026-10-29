"""Compare a narration splice to the approved script.

Picture cuts are not audio splices. A heard word is a fragment only when it
is not in the expected text near that cut, after number words and digits are
folded together and fuzzy or phonetic matches are accepted. Low-confidence
mishearings are ignored.

Episode 1 v8 left "It's", the start of removed line 00d_b1, at about 17.48s
on the splice near 18.045s. The scripted "if" in "And if one comes down" is
not that bug.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from ytp.plan import EditPlan
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

    The plan has line times, not word times. Even spacing is enough to decide
    which words belong in a splice window.
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


_ONES = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
}
_TEENS = {
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}
_TENS = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
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


def canonicalize_tokens(tokens: list[str]) -> list[str]:
    """Fold number words and digits so 'Tip one' and '1' compare equal."""
    folded: list[str] = []
    index = 0
    while index < len(tokens):
        raw = tokens[index]
        token = normalize_token(raw)
        if not token:
            index += 1
            continue
        if token.isdigit():
            folded.append(str(int(token)))
            index += 1
            continue
        hyphen = raw.casefold().strip("'")
        if "-" in hyphen:
            left, right = hyphen.split("-", 1)
            if left in _TENS and right in _ONES:
                folded.append(str(_TENS[left] + _ONES[right]))
                index += 1
                continue
        if token in _TENS and index + 1 < len(tokens):
            nxt = normalize_token(tokens[index + 1])
            if nxt in _ONES:
                folded.append(str(_TENS[token] + _ONES[nxt]))
                index += 2
                continue
        if token in _ONES:
            folded.append(str(_ONES[token]))
        elif token in _TEENS:
            folded.append(str(_TEENS[token]))
        elif token in _TENS:
            folded.append(str(_TENS[token]))
        else:
            folded.append(token)
        index += 1
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


def _close_to_expected(token: str, expected: list[str]) -> bool:
    for exp in expected:
        if token == exp:
            return True
        if len(token) >= 3 and len(exp) >= 3:
            if SequenceMatcher(a=token, b=exp, autojunk=False).ratio() >= 0.6:
                return True
            if _soundex(token) == _soundex(exp) and not _soundex(token).endswith("000"):
                return True
        if len(exp) >= 4 and token.startswith(exp):
            return True
        if len(token) >= 4 and exp.startswith(token) and len(token) / len(exp) >= 0.6:
            return True
    return False


def _is_clipped_prefix(token: str, expected: list[str]) -> bool:
    if len(token) < 2:
        return False
    return any(exp.startswith(token) and token != exp and len(token) < 4 for exp in expected)


def compare_window(expected: list[Word], actual: list[Word]) -> list[tuple[str, Word]]:
    """Heard words that are not in the approved text near this splice.

    Number words match digits. Fuzzy and phonetic neighbors match. A word
    below the confidence floor is a mishearing and is not a finding.
    """
    raw: list[str] = []
    for word in expected:
        raw.extend(tokenize(word.text))
    expected_tokens = canonicalize_tokens(raw)
    extras: list[tuple[str, Word]] = []
    for word in actual:
        if word.confidence is not None and word.confidence < CONFIDENCE_MIN:
            continue
        heard = canonicalize_tokens(tokenize(word.text))
        if not heard:
            continue
        token = heard[0] if len(heard) == 1 else " ".join(heard)
        if _close_to_expected(token, expected_tokens):
            continue
        if len(heard) == 1 and _is_clipped_prefix(heard[0], expected_tokens):
            extras.append(("partial_word", word))
        else:
            extras.append(("stray_fragment", word))
    return extras
