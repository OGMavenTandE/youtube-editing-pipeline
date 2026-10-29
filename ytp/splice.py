"""Compare a splice window to the approved narration.

Episode 1 v8 shipped with a stray "if" left from a partial sentence cut.
A window that contains a word the script does not have there, or only a
prefix of a word, fails.
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
    """Narration edges and picture edges, collapsed when they are one cut."""
    times: list[float] = []
    for line in plan.narration:
        times.append(line.start)
        times.append(line.end)
    for segment in plan.segments:
        times.append(segment.start)
        times.append(segment.start + segment.duration)
    return _cluster([time for time in times if time >= 0], gap)


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


def words_overlapping(words: list[Word], start: float, end: float) -> list[Word]:
    return [word for word in words if word.end > start and word.start < end]


def classify_extra(token: str, expected: list[str]) -> str:
    """A clipped word is a proper prefix or suffix of a word that should be here."""
    for exp in expected:
        if not token or not exp or token == exp:
            continue
        if len(exp) > len(token) and (exp.startswith(token) or exp.endswith(token)):
            if len(token) >= 2:
                return "partial_word"
        if len(token) > len(exp) >= 2 and (token.startswith(exp) or token.endswith(exp)):
            return "partial_word"
    return "stray_fragment"


def compare_window(expected: list[Word], actual: list[Word]) -> list[tuple[str, Word]]:
    """Extras in `actual` that the approved words do not account for.

    Returns ``(code, word)`` with code ``partial_word`` or ``stray_fragment``.
    Words the recognizer missed are not flagged; the bug is audio that is
    still there.
    """
    expected_tokens = [
        token for token in (normalize_token(word.text) for word in expected) if token
    ]
    actual_words: list[Word] = []
    actual_tokens: list[str] = []
    for word in actual:
        token = normalize_token(word.text)
        if not token:
            continue
        actual_words.append(word)
        actual_tokens.append(token)
    if not actual_tokens:
        return []
    matcher = SequenceMatcher(a=expected_tokens, b=actual_tokens, autojunk=False)
    extras: list[tuple[str, Word]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal" or tag == "delete":
            continue
        nearby = expected_tokens[max(0, i1 - 3) : min(len(expected_tokens), i2 + 3)]
        if tag == "replace":
            pool = list(expected_tokens[i1:i2])
            for index in range(j1, j2):
                token = actual_tokens[index]
                if token in pool:
                    pool.remove(token)
                    continue
                extras.append((classify_extra(token, nearby or pool), actual_words[index]))
            continue
        for index in range(j1, j2):
            extras.append((classify_extra(actual_tokens[index], nearby), actual_words[index]))
    return extras
