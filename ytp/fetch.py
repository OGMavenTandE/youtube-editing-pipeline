"""`ytp transcribe`: public video links or local files to timestamped transcripts.

Downloads use yt-dlp (public posts only; no cookies, no browser sessions).
Transcription is the same local faster-whisper code as `ytp take`, so a bot
that calls this spends no tokens on the audio.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO
from urllib.parse import parse_qs, urlparse

from ytp.captions import format_srt_time
from ytp.ff import FFmpegError, ffmpeg_bin, probe_duration
from ytp.take import transcript_rows, transcript_text
from ytp.transcribe import FasterWhisperTranscriber, Transcriber, Transcript, _cuda_or_cpu

# Fast on the RTX 4070 SUPER (float16). The CPU fallback uses a smaller model.
GPU_MODEL = "large-v3-turbo"
CPU_MODEL = "small"
DEFAULT_OUT = Path("transcripts")
LOG_NAME = "transcribe.log"

_URL_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_BARE_X_RE = re.compile(r"^(?:www\.|mobile\.)?(?:x|twitter)\.com/", re.IGNORECASE)
_X_HOSTS = {"x.com", "twitter.com", "mobile.twitter.com", "mobile.x.com", "fxtwitter.com",
            "vxtwitter.com", "fixupx.com"}
_X_STATUS_RE = re.compile(r"^/(?:(?P<user>[^/]+)/)?(?:status|statuses)/(?P<id>\d+)")
# Windows reserved device names cannot be folder names.
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
             *(f"lpt{i}" for i in range(1, 10))}


class TranscribeError(RuntimeError):
    """A failure with a short reason for the FAIL line."""


@dataclass
class Item:
    raw: str
    kind: str  # "url" or "file"
    value: str  # normalized URL, or the file path text

    @property
    def is_url(self) -> bool:
        return self.kind == "url"


@dataclass
class Result:
    item: Item
    ok: bool
    folder: Path | None = None
    transcript: Path | None = None
    duration: float = 0.0
    reason: str = ""
    lines: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- inputs


def classify(text: str) -> Item:
    """URL or local file. A bare ``x.com/...`` link counts as a URL."""
    raw = text.strip().strip('"').strip("'")
    if _URL_RE.match(raw):
        return Item(text, "url", raw)
    if _BARE_X_RE.match(raw):
        return Item(text, "url", "https://" + raw)
    return Item(text, "file", raw)


def read_url_file(path: Path) -> list[str]:
    """One link or path per line. Blank lines and lines starting with # are skipped."""
    entries: list[str] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            entries.append(line)
    return entries


# --------------------------------------------------------------------------- naming


def sanitize_name(text: str, limit: int = 80) -> str:
    """A folder name that is valid on Windows and Linux."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", text)
    name = re.sub(r"\s+", "-", name)
    name = re.sub(r"-{2,}", "-", name).strip(" .-")
    name = name[:limit].rstrip(" .-")
    if not name:
        name = "video"
    if name.split(".")[0].lower() in _RESERVED:
        name = "_" + name
    return name


def x_post(url: str) -> tuple[str, str] | None:
    """(handle, post id) for an X/Twitter status link, else None. handle may be ""."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in _X_HOSTS:
        return None
    match = _X_STATUS_RE.match(parsed.path)
    if not match:
        return None
    user = match.group("user") or ""
    if user.lower() in {"i", "i/web"}:
        user = ""
    return user, match.group("id")


def folder_name(item: Item) -> str:
    """Per-video folder: ``x-<handle>-<post id>`` for X, else host and video id."""
    if not item.is_url:
        return sanitize_name(Path(item.value).stem)
    post = x_post(item.value)
    if post is not None:
        user, post_id = post
        return sanitize_name(f"x-{user}-{post_id}" if user else f"x-{post_id}")
    parsed = urlparse(item.value)
    host = (parsed.hostname or "link").lower()
    if host.startswith("www."):
        host = host[4:]
    host = host.split(".")[0] if host.count(".") >= 1 else host
    video_id = (parse_qs(parsed.query).get("v") or [""])[0]
    if not video_id:
        parts = [part for part in parsed.path.split("/") if part]
        video_id = parts[-1] if parts else ""
    if not video_id:
        video_id = hashlib.sha1(item.value.encode("utf-8")).hexdigest()[:10]
    return sanitize_name(f"{host}-{video_id}")


# --------------------------------------------------------------------------- errors


_REASONS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("no video could be found", "no video formats", "no media found", "there's no video",
      "does not contain a video", "no formats found", "requested format is not available"),
     "no video found (text-only, deleted, or protected post)"),
    (("protected", "private"),
     "protected or private post; needs a public post"),
    (("login", "log in", "sign in", "authenticat", "cookies", "nsfw", "age-restricted",
      "age restricted", "sensitive"),
     "login required; needs a public post"),
    (("404", "not found", "does not exist", "deleted", "suspended", "unavailable",
      "has been removed", "no status found", "tombstone"),
     "post deleted or unavailable"),
    (("unsupported url",), "unsupported link"),
    (("ffmpeg", "ffprobe"), "ffmpeg not found; install ffmpeg or set YTP_FFMPEG"),
    (("timed out", "connection", "network", "getaddrinfo", "temporary failure", "ssl"),
     "network error"),
)


def short_reason(exc: BaseException) -> str:
    """One short reason for the FAIL line. The full error goes to transcribe.log."""
    if isinstance(exc, TranscribeError):
        return str(exc)
    message = str(exc).lower()
    for needles, reason in _REASONS:
        if any(needle in message for needle in needles):
            return reason
    first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    first = re.sub(r"^error:\s*", "", first, flags=re.IGNORECASE)
    first = re.sub(r"^\[[^\]]+\]\s*(\S+:\s*)?", "", first)
    return first[:120]


# --------------------------------------------------------------------------- download


def _load_yt_dlp():
    try:
        import yt_dlp
    except ImportError as exc:
        raise TranscribeError(
            "yt-dlp is not installed; pip install -e \".[whisper]\" in the ytp venv"
        ) from exc
    return yt_dlp


class _YdlLogger:
    def __init__(self, log: Log) -> None:
        self.log = log

    def debug(self, msg: str) -> None:
        if not msg.startswith("[debug] "):
            self.log.write(msg)

    def info(self, msg: str) -> None:
        self.log.write(msg)

    def warning(self, msg: str) -> None:
        self.log.write(f"WARNING: {msg}")

    def error(self, msg: str) -> None:
        self.log.write(msg)


def download(url: str, folder: Path, ffmpeg: str, log: Log) -> tuple[Path, dict]:
    """Best audio for one public post into ``folder/media.<ext>``. Returns (file, info)."""
    yt_dlp = _load_yt_dlp()
    folder = folder.resolve()
    folder.mkdir(parents=True, exist_ok=True)
    options = {
        "format": "bestaudio/best",
        "outtmpl": str(folder / "media.%(ext)s"),
        "ffmpeg_location": ffmpeg,
        "noplaylist": True,
        # A post with several videos is a playlist. Take the first.
        "playlist_items": "1",
        "quiet": True,
        "no_warnings": False,
        "noprogress": True,
        "logger": _YdlLogger(log),
        "overwrites": True,
        "restrictfilenames": True,
        "windowsfilenames": True,
        "socket_timeout": 30,
        "retries": 3,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        if info is None:
            raise TranscribeError("no video in this post")
        entries = info.get("entries")
        if entries is not None:
            entries = [entry for entry in entries if entry]
            if not entries:
                raise TranscribeError("no video in this post")
            count = info.get("playlist_count") or len(entries)
            if count and count > 1:
                log.write(f"post has {count} videos; transcribed the first")
            info = entries[0]
        media = _downloaded_path(ydl, info, folder)
    if media is None or not media.is_file():
        raise TranscribeError("download produced no media file")
    return media, info


def _downloaded_path(ydl, info: dict, folder: Path) -> Path | None:
    for record in info.get("requested_downloads") or []:
        path = record.get("filepath") or record.get("_filename")
        if path and Path(path).is_file():
            return Path(path)
    for key in ("filepath", "_filename"):
        path = info.get(key)
        if path and Path(path).is_file():
            return Path(path)
    try:
        path = Path(ydl.prepare_filename(info))
        if path.is_file():
            return path
    except Exception:
        pass
    found = sorted(
        (p for p in folder.glob("media.*") if p.suffix not in {".part", ".ytdl", ".json"}),
        key=lambda p: p.stat().st_mtime,
    )
    return found[-1] if found else None


_PARTIAL_SUFFIXES = {".part", ".ytdl", ".json", ".temp", ".tmp"}


def existing_media(folder: Path) -> Path | None:
    """A finished download from an earlier run (``media.<ext>``), else None."""
    if not folder.is_dir():
        return None
    found = [
        path
        for path in folder.glob("media.*")
        if path.is_file()
        and path.stat().st_size > 0
        and not (set(s.lower() for s in path.suffixes) & _PARTIAL_SUFFIXES)
    ]
    found.sort(key=lambda path: path.stat().st_mtime)
    return found[-1] if found else None


def cached_info(folder: Path) -> dict:
    """yt-dlp fields from the earlier run's source.json, so a reuse keeps the metadata."""
    path = folder / "source.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        "id": data.get("id") or "",
        "extractor_key": data.get("extractor") or "",
        "uploader": data.get("uploader") or "",
        "uploader_id": data.get("handle") or "",
        "title": data.get("title") or "",
        "description": data.get("description") or "",
        "upload_date": data.get("upload_date") or "",
        "duration": data.get("duration") or 0.0,
        "webpage_url": data.get("webpage_url") or "",
    }


def mark_failed(folder: Path) -> Path:
    """Rename a failed run's folder to ``<name>-failed`` so it never looks like a success.

    A folder that already holds a transcript from an earlier good run is left alone.
    An older ``-failed`` folder for the same input is updated in place; its log is appended.
    """
    if not folder.is_dir() or (folder / "transcript.txt").is_file():
        return folder
    target = folder.with_name(folder.name + "-failed")
    try:
        if target.exists():
            for child in folder.iterdir():
                dest = target / child.name
                if child.name == LOG_NAME and dest.is_file():
                    # keep the history of every failed attempt in one log
                    with dest.open("a", encoding="utf-8", newline="\n") as handle:
                        handle.write(child.read_text(encoding="utf-8"))
                    child.unlink()
                else:
                    child.replace(dest)
            folder.rmdir()
        else:
            folder.rename(target)
    except OSError:
        return folder
    return target


def source_record(item: Item, info: dict | None, media: Path | None, duration: float) -> dict:
    info = info or {}
    upload = str(info.get("upload_date") or "")
    if re.fullmatch(r"\d{8}", upload):
        upload = f"{upload[:4]}-{upload[4:6]}-{upload[6:]}"
    post = x_post(item.value) if item.is_url else None
    handle = info.get("uploader_id") or info.get("channel_id") or ""
    if not handle and post is not None:
        handle = post[0]
    return {
        "url": item.value if item.is_url else None,
        "file": None if item.is_url else str(Path(item.value).resolve()),
        "webpage_url": info.get("webpage_url") or (item.value if item.is_url else None),
        "post_id": post[1] if post else "",
        "id": info.get("id") or "",
        "extractor": info.get("extractor_key") or info.get("extractor") or "",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "handle": handle,
        "title": info.get("title") or (Path(item.value).stem if not item.is_url else ""),
        "description": info.get("description") or "",
        "upload_date": upload,
        "duration": round(float(duration), 3),
        "media": media.name if media is not None else None,
    }


# --------------------------------------------------------------------------- outputs


def srt_text(transcript: Transcript) -> str:
    blocks = []
    for index, (start, end, text) in enumerate(transcript_rows(transcript), start=1):
        end = max(end, start + 0.5)
        blocks.append(f"{index}\n{format_srt_time(start)} --> {format_srt_time(end)}\n{text}\n")
    return "\n".join(blocks)


def transcript_payload(
    transcript: Transcript,
    *,
    model: str,
    device: str,
    duration: float,
    compute_type: str = "",
) -> dict:
    return {
        "model": model,
        "device": device,
        "compute_type": compute_type,
        "language": transcript.language or "",
        "duration": round(float(duration), 3),
        "text": transcript.text,
        "segments": [
            {"start": round(start, 3), "end": round(end, 3), "text": text}
            for start, end, text in transcript_rows(transcript)
        ],
    }


def write_outputs(
    folder: Path,
    transcript: Transcript,
    source: dict,
    *,
    model: str,
    device: str,
    duration: float,
    compute_type: str = "",
) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    txt = folder / "transcript.txt"
    txt.write_text(transcript_text(transcript), encoding="utf-8", newline="\n")
    (folder / "transcript.srt").write_text(srt_text(transcript), encoding="utf-8", newline="\n")
    payload = transcript_payload(
        transcript, model=model, device=device, duration=duration, compute_type=compute_type
    )
    (folder / "transcript.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )
    (folder / "source.json").write_text(
        json.dumps(source, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )
    return txt


def format_duration(seconds: float) -> str:
    """``m:ss`` or ``h:mm:ss``. Always the last token on the OK line."""
    total = int(round(max(0.0, float(seconds))))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


# --------------------------------------------------------------------------- logging


class Log:
    """Per-video log. Quiet mode writes only to the file; otherwise also to stderr."""

    def __init__(self, quiet: bool, stream: TextIO | None = None) -> None:
        self.quiet = quiet
        self.stream = stream if stream is not None else sys.stderr
        self.handle: TextIO | None = None
        self.pending: list[str] = []

    def open(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = path.open("a", encoding="utf-8", newline="\n")
        for line in self.pending:
            self.handle.write(line + "\n")
        self.pending = []
        self.handle.flush()

    def write(self, message: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"{stamp} {message}"
        if self.handle is not None:
            self.handle.write(line + "\n")
            self.handle.flush()
        else:
            self.pending.append(line)
        if not self.quiet:
            print(f"ytp: {message}", file=self.stream, flush=True)

    # file-like, so quiet mode can send stray prints and progress bars here
    def flush(self) -> None:
        if self.handle is not None:
            self.handle.flush()

    def close(self) -> None:
        if self.handle is not None:
            self.handle.close()
            self.handle = None


class _LogStream:
    """Captures print()/tqdm output in quiet mode so stdout stays one line per input."""

    def __init__(self, log: Log) -> None:
        self.log = log
        self.buffer = ""

    def write(self, text: str) -> int:
        self.buffer += text.replace("\r", "\n")
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            if line.strip():
                self.log.write(line.rstrip())
        return len(text)

    def flush(self) -> None:
        if self.buffer.strip():
            self.log.write(self.buffer.rstrip())
        self.buffer = ""

    def isatty(self) -> bool:
        return False


# --------------------------------------------------------------------------- run


@dataclass
class Options:
    out: Path = DEFAULT_OUT
    model: str = "auto"
    device: str = "auto"
    language: str = "en"
    keep_media: bool = True
    quiet: bool = False
    summary_lines: int = 0
    refresh: bool = False
    vad: bool = False


class Engine:
    """One loaded Whisper model, reused for every input. Falls back to CPU once."""

    def __init__(self, options: Options, factory=None) -> None:
        self.options = options
        self.factory = factory
        self._transcriber: Transcriber | None = None
        self.device = ""
        self.model = ""
        self.compute_type = ""

    def _make(self, device: str) -> Transcriber:
        model = self.options.model
        if model == "auto":
            model = GPU_MODEL if device == "cuda" else CPU_MODEL
        self.device = device
        self.model = model
        self.compute_type = "float16" if device == "cuda" else "int8"
        factory = self.factory or FasterWhisperTranscriber
        return factory(model, device, self.options.language, quick=True, vad=self.options.vad)

    def _load(self, log: Log) -> None:
        """Load the model before transcribing so the log shows compute type and load time."""
        transcriber = self._transcriber
        load = getattr(transcriber, "load", None)
        started = time.monotonic()
        if callable(load):
            load()
        seconds = getattr(transcriber, "load_seconds", None)
        if not isinstance(seconds, (int, float)) or not seconds:
            seconds = time.monotonic() - started
        compute = getattr(transcriber, "compute_type", "") or self.compute_type
        self.compute_type = compute
        log.write(
            f"whisper model={self.model} device={self.device} compute_type={compute} "
            f"load={seconds:.1f}s"
        )

    def _run(self, media: Path, log: Log) -> Transcript:
        self._load(log)
        transcript = self._transcriber.transcribe(media)
        dropped = getattr(self._transcriber, "dropped_segments", 0)
        if isinstance(dropped, int) and dropped:
            log.write(f"dropped {dropped} hallucinated segment(s) (stock lines or loops)")
        return transcript

    def transcribe(self, media: Path, log: Log) -> Transcript:
        if self._transcriber is None:
            device = self.options.device
            if device == "auto":
                device = _cuda_or_cpu()
                if device != "cuda":
                    log.write("warning: CUDA is not available; transcribing on CPU int8")
                    device = "cpu"
            self._transcriber = self._make(device)
        try:
            return self._run(media, log)
        except RuntimeError as exc:
            if self.device == "cuda" and self.options.device == "auto" and (
                "faster-whisper" not in str(exc)
            ):
                log.write(f"warning: CUDA transcription failed ({exc}); retrying on CPU int8")
                self._transcriber = self._make("cpu")
                return self._run(media, log)
            raise


def _unique_folder(root: Path, name: str, used: set[Path]) -> Path:
    folder = root / name
    counter = 2
    while folder.resolve() in used:
        folder = root / f"{name}-{counter}"
        counter += 1
    used.add(folder.resolve())
    return folder


def process(item: Item, folder: Path, engine: Engine, options: Options, log: Log) -> Result:
    started = time.monotonic()
    log.write(f"input {item.value}")
    info: dict | None = None
    downloaded: Path | None = None
    try:
        try:
            ffmpeg = ffmpeg_bin()
        except FFmpegError as exc:
            raise TranscribeError("ffmpeg not found; install ffmpeg or set YTP_FFMPEG") from exc
        cached = existing_media(folder) if item.is_url else None
        if cached is not None and options.keep_media and not options.refresh:
            media = cached
            info = cached_info(folder)
            log.write(f"reusing {media.name} (pass --refresh to download again)")
        elif item.is_url:
            media, info = download(item.value, folder, ffmpeg, log)
            downloaded = media
            log.write(f"downloaded {media.name}")
        else:
            media = Path(item.value)
            if not media.is_file():
                raise TranscribeError("file not found")
        duration = float((info or {}).get("duration") or 0.0)
        if not duration:
            try:
                duration = probe_duration(media)
            except (FFmpegError, ValueError) as exc:
                log.write(f"warning: could not probe duration ({exc})")
        transcript = engine.transcribe(media, log)
        if not duration and transcript.segments:
            duration = max(end for _start, end, _text in transcript.segments)
        source = source_record(item, info, media if item.is_url else None, duration)
        if item.is_url and not options.keep_media:
            source["media"] = None
        path = write_outputs(
            folder,
            transcript,
            source,
            model=engine.model,
            device=engine.device,
            compute_type=engine.compute_type,
            duration=duration,
        )
        lines = transcript_text(transcript).splitlines()
        log.write(
            f"wrote {path} ({len(lines)} line(s), {format_duration(duration)} of media, "
            f"{time.monotonic() - started:.1f}s)"
        )
        return Result(item, True, folder, path, duration, lines=lines)
    except Exception as exc:  # one bad link must not stop the rest
        reason = short_reason(exc)
        log.write(f"FAIL {reason}: {type(exc).__name__}: {exc}")
        return Result(item, False, folder, reason=reason)
    finally:
        if downloaded is not None and not options.keep_media:
            downloaded.unlink(missing_ok=True)
            log.write(f"removed {downloaded.name} (--no-keep-media)")


def result_lines(result: Result, summary_lines: int = 0) -> list[str]:
    if not result.ok:
        reason = " ".join(result.reason.split()) or "failed"
        if result.folder is not None and (result.folder / LOG_NAME).is_file():
            reason += f"; log {(result.folder / LOG_NAME).resolve()}"
        return [f"FAIL {result.item.value} {reason}"]
    lines = [f"OK {result.transcript.resolve()} {format_duration(result.duration)}"]
    if summary_lines > 0:
        lines.extend(result.lines[:summary_lines])
    return lines


def run(
    inputs: list[str],
    options: Options,
    *,
    engine: Engine | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Transcribe every input. Returns 0 when all succeeded, 1 when any failed."""
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr
    engine = engine or Engine(options)
    root = options.out
    used: set[Path] = set()
    failed = 0
    for text in inputs:
        item = classify(text)
        folder = _unique_folder(root, folder_name(item), used)
        log = Log(options.quiet, stderr)
        try:
            log.open(folder / LOG_NAME)
        except OSError as exc:
            result = Result(item, False, folder, reason=f"cannot write {folder}: {exc.strerror}")
        else:
            if options.quiet:
                sink = _LogStream(log)
                with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                    result = process(item, folder, engine, options, log)
                sink.flush()
            else:
                result = process(item, folder, engine, options, log)
        finally:
            log.close()
        if not result.ok:
            failed += 1
            result.folder = mark_failed(folder)
        for line in result_lines(result, options.summary_lines):
            print(line, file=stdout, flush=True)
    return 1 if failed else 0
