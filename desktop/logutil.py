"""Sanitize UI log lines so secrets never appear."""

from __future__ import annotations

_SECRET_MARKERS = (
    "GEMINI_API_KEY",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "client_secret",
    "refresh_token",
    "access_token",
)


def sanitize_log_line(line: str) -> str:
    text = line.replace("\r", "").rstrip("\n")
    upper = text.upper()
    if any(marker.upper() in upper for marker in _SECRET_MARKERS):
        return "[redacted]"
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
