"""Sanitize UI log lines so secret values never appear.

Key *names* (GEMINI_API_KEY, client_secret, access_token) are not enough
to hide a line. User-facing errors such as "Gemini API key is not set"
must stay readable. Only assignments and secret *values* are redacted.
"""

from __future__ import annotations

import re

_ASSIGNMENT_RE = re.compile(
    r"(?i)\b("
    r"GEMINI_API_KEY|"
    r"GOOGLE_OAUTH_CLIENT_SECRET|"
    r"GOOGLE_OAUTH_CLIENT_ID|"
    r"refresh_token|"
    r"access_token|"
    r"client_secret"
    r""")\b\s*["']?\s*[:=]\s*["']?[^\s"']+["']?"""
)
_AIZA_RE = re.compile(r"AIza[0-9A-Za-z_\-]{20,}")
_OAUTH_ACCESS_RE = re.compile(r"ya29\.[0-9A-Za-z._\-]+")
_OAUTH_REFRESH_RE = re.compile(r"1//[0-9A-Za-z_\-]+")
_LONG_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9+/=_\-])[A-Za-z0-9+/=_\-]{40,}(?![A-Za-z0-9+/=_\-])")


def sanitize_log_line(line: str) -> str:
    text = line.replace("\r", "").rstrip("\n")
    text = _ASSIGNMENT_RE.sub("[redacted]", text)
    text = _AIZA_RE.sub("[redacted]", text)
    text = _OAUTH_ACCESS_RE.sub("[redacted]", text)
    text = _OAUTH_REFRESH_RE.sub("[redacted]", text)
    text = _LONG_TOKEN_RE.sub("[redacted]", text)
    return text


class LogWriter:
    """File-like stdout that forwards sanitized lines to a callback."""

    def __init__(self, callback: object) -> None:
        self._callback = callback
        self._buf = ""

    def write(self, data: str) -> int:
        if not data:
            return 0
        self._buf += str(data)
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            cleaned = sanitize_log_line(line)
            if cleaned:
                self._callback(cleaned)
        return len(data)

    def flush(self) -> None:
        if self._buf.strip():
            cleaned = sanitize_log_line(self._buf)
            if cleaned:
                self._callback(cleaned)
        self._buf = ""
