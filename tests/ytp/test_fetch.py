"""`ytp transcribe` with yt-dlp and Whisper mocked. No network, no model download."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from ytp import cli, fetch
from ytp.transcribe import Transcriber, Transcript, Word


class FakeTranscriber(Transcriber):
    def __init__(self, model: str, device: str, language: str) -> None:
        self.model = model
        self.device = device
        self.language = language
        self.name = model
        self.calls: list[Path] = []

    def transcribe(self, media, *, start=None, end=None) -> Transcript:
        self.calls.append(Path(media))
        print("noise from a progress bar")  # quiet mode must keep this off stdout
        print("more noise", file=sys.stderr)
        return Transcript(
            text="Hello there. Second line here.",
            words=[Word("Hello", 0.0, 0.4), Word("there.", 0.4, 1.0)],
            language="en",
            segments=[(0.0, 1.0, "Hello there."), (1.2, 2.5, "Second line here.")],
        )


class FakeYoutubeDL:
    """Stands in for yt_dlp.YoutubeDL. Behaviour is keyed on the post id in the URL."""

    seen_options: list[dict] = []

    def __init__(self, options: dict) -> None:
        self.options = options
        FakeYoutubeDL.seen_options.append(options)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url: str, download: bool = True):
        if "/status/404" in url:
            raise RuntimeError("ERROR: [twitter] 404: Requested tweet is unavailable")
        if "/status/401" in url:
            raise RuntimeError(
                "ERROR: [twitter] 401: NSFW tweet requires authentication. Use --cookies"
            )
        if "/status/0" in url:
            raise RuntimeError("ERROR: [twitter] 0: No video could be found in this tweet")
        assert download
        path = Path(self.options["outtmpl"].replace("%(ext)s", "m4a"))
        path.write_bytes(b"fake audio")
        return {
            "id": "999",
            "extractor_key": "Twitter",
            "uploader": "Scott Mastin",
            "uploader_id": "ScottTestsAI",
            "title": "A test post",
            "upload_date": "20261029",
            "duration": 83.4,
            "webpage_url": url,
            "requested_downloads": [{"filepath": str(path)}],
        }

    def prepare_filename(self, info):  # pragma: no cover - requested_downloads wins
        return ""


class FakeYtDlp:
    YoutubeDL = FakeYoutubeDL


@pytest.fixture
def mocked(monkeypatch, tmp_path):
    FakeYoutubeDL.seen_options = []
    monkeypatch.setattr(fetch, "_load_yt_dlp", lambda: FakeYtDlp)
    monkeypatch.setattr(fetch, "ffmpeg_bin", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(fetch, "probe_duration", lambda path: 12.0)
    monkeypatch.setattr(fetch, "_cuda_or_cpu", lambda: "cuda")
    made: list[FakeTranscriber] = []

    def factory(model, device, language):
        made.append(FakeTranscriber(model, device, language))
        return made[-1]

    monkeypatch.setattr(fetch, "FasterWhisperTranscriber", factory)
    return made


def _run(inputs, tmp_path, **kw):
    out = io.StringIO()
    err = io.StringIO()
    options = fetch.Options(out=tmp_path / "out", **kw)
    code = fetch.run(inputs, options, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------- detection and naming


@pytest.mark.parametrize(
    ("text", "kind", "value"),
    [
        ("https://x.com/a/status/1", "url", "https://x.com/a/status/1"),
        ("http://twitter.com/a/status/1", "url", "http://twitter.com/a/status/1"),
        ("x.com/a/status/1", "url", "https://x.com/a/status/1"),
        ("twitter.com/a/status/1", "url", "https://twitter.com/a/status/1"),
        ('"https://youtu.be/abc"', "url", "https://youtu.be/abc"),
        (r"C:\AI Eval\YouTube\desk.mp4", "file", r"C:\AI Eval\YouTube\desk.mp4"),
        ("clip.mp4", "file", "clip.mp4"),
        ("x.com.mp4", "file", "x.com.mp4"),
    ],
)
def test_classify_url_vs_file(text, kind, value):
    item = fetch.classify(text)
    assert item.kind == kind
    assert item.value == value


@pytest.mark.parametrize(
    ("url", "name"),
    [
        (
            "https://x.com/ScottTestsAI/status/1850000000000000001",
            "x-ScottTestsAI-1850000000000000001",
        ),
        ("https://twitter.com/jack/status/20?s=20", "x-jack-20"),
        ("https://x.com/i/status/123", "x-123"),
        ("https://mobile.twitter.com/a_b/status/5/video/1", "x-a_b-5"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube-dQw4w9WgXcQ"),
        ("https://vimeo.com/76979871", "vimeo-76979871"),
    ],
)
def test_folder_name_from_post_id(url, name):
    assert fetch.folder_name(fetch.classify(url)) == name


def test_folder_name_for_file_and_windows_safety():
    assert fetch.folder_name(fetch.classify("/takes/desk take.mp4")) == "desk-take"
    assert fetch.sanitize_name('a<b>c:"d|e?f*g') == "a-b-c-d-e-f-g"
    assert fetch.sanitize_name("con") == "_con"
    assert fetch.sanitize_name("name. ") == "name"
    assert fetch.sanitize_name("") == "video"
    assert len(fetch.sanitize_name("x" * 300)) == 80


def test_read_url_file_skips_comments(tmp_path):
    path = tmp_path / "urls.txt"
    path.write_text("\ufeff# links\nhttps://x.com/a/status/1\n\n  x.com/b/status/2  \n", "utf-8")
    assert fetch.read_url_file(path) == ["https://x.com/a/status/1", "x.com/b/status/2"]


@pytest.mark.parametrize(
    ("message", "reason"),
    [
        ("ERROR: [twitter] 1: No video could be found in this tweet", "no video found"),
        ("NSFW tweet requires authentication", "login required; needs a public post"),
        ("Sign in to confirm your age", "login required; needs a public post"),
        ("This account is protected", "protected or private post; needs a public post"),
        ("HTTP Error 404: Not Found", "post deleted or unavailable"),
        ("Unsupported URL: https://example.com/", "unsupported link"),
    ],
)
def test_short_reason(message, reason):
    assert fetch.short_reason(RuntimeError(message)).startswith(reason)


# ---------------------------------------------------------------- runs


def test_quiet_url_writes_all_outputs_and_one_line(mocked, tmp_path):
    url = "https://x.com/ScottTestsAI/status/1850000000000000001"
    code, out, err = _run([url], tmp_path, quiet=True)
    assert code == 0
    assert err == ""
    folder = (tmp_path / "out" / "x-ScottTestsAI-1850000000000000001").resolve()
    assert out == f"OK {folder / 'transcript.txt'} 1:23\n"
    assert (folder / "transcript.txt").read_text("utf-8") == (
        "[0:00.00-0:01.00] Hello there.\n[0:01.20-0:02.50] Second line here.\n"
    )
    srt = (folder / "transcript.srt").read_text("utf-8")
    assert srt.startswith("1\n00:00:00,000 --> 00:00:01,000\nHello there.\n\n2\n")
    payload = json.loads((folder / "transcript.json").read_text("utf-8"))
    assert payload["model"] == fetch.GPU_MODEL and payload["device"] == "cuda"
    assert payload["segments"][1] == {"start": 1.2, "end": 2.5, "text": "Second line here."}
    source = json.loads((folder / "source.json").read_text("utf-8"))
    assert source["url"] == url
    assert source["post_id"] == "1850000000000000001"
    assert source["handle"] == "ScottTestsAI"
    assert source["uploader"] == "Scott Mastin"
    assert source["title"] == "A test post"
    assert source["upload_date"] == "2026-10-29"
    assert source["duration"] == 83.4
    assert (folder / "media.m4a").is_file()  # kept by default
    log = (folder / fetch.LOG_NAME).read_text("utf-8")
    assert "noise from a progress bar" in log and "more noise" in log
    assert FakeYoutubeDL.seen_options[0]["ffmpeg_location"] == "/usr/bin/ffmpeg"
    assert "cookiefile" not in FakeYoutubeDL.seen_options[0]
    assert "cookiesfrombrowser" not in FakeYoutubeDL.seen_options[0]


def test_partial_failure_keeps_going_and_exits_1(mocked, tmp_path):
    inputs = [
        "https://x.com/a/status/404",
        "https://x.com/a/status/7",
        "https://x.com/a/status/401",
        "https://x.com/a/status/0",
        str(tmp_path / "missing.mp4"),
        "https://x.com/b/status/8",
    ]
    code, out, _err = _run(inputs, tmp_path, quiet=True)
    assert code == 1
    lines = out.splitlines()
    assert len(lines) == 6
    assert lines[0] == "FAIL https://x.com/a/status/404 post deleted or unavailable"
    folder = (tmp_path / "out" / "x-a-7").resolve()
    assert lines[1] == f"OK {folder / 'transcript.txt'} 1:23"
    assert lines[2] == "FAIL https://x.com/a/status/401 login required; needs a public post"
    assert lines[3].startswith("FAIL https://x.com/a/status/0 no video found")
    assert lines[4] == f"FAIL {tmp_path / 'missing.mp4'} file not found"
    assert lines[5].startswith("OK ")
    # one model load for the whole batch
    assert len(mocked) == 1 and len(mocked[0].calls) == 2


def test_local_file_and_summary_lines(mocked, tmp_path):
    media = tmp_path / "desk take.mp4"
    media.write_bytes(b"x")
    code, out, _ = _run([str(media)], tmp_path, quiet=True, summary_lines=1)
    assert code == 0
    folder = (tmp_path / "out" / "desk-take").resolve()
    assert out.splitlines() == [
        f"OK {folder / 'transcript.txt'} 0:12",
        "[0:00.00-0:01.00] Hello there.",
    ]
    source = json.loads((folder / "source.json").read_text("utf-8"))
    assert source["file"] == str(media.resolve()) and source["url"] is None
    assert media.is_file()  # never removed, even with --no-keep-media


def test_no_keep_media_deletes_download(mocked, tmp_path):
    code, _out, _ = _run(["https://x.com/a/status/7"], tmp_path, quiet=True, keep_media=False)
    assert code == 0
    folder = tmp_path / "out" / "x-a-7"
    assert not list(folder.glob("media.*"))
    assert json.loads((folder / "source.json").read_text("utf-8"))["media"] is None


def test_missing_ffmpeg_is_a_clear_failure(mocked, monkeypatch, tmp_path):
    def boom():
        raise fetch.FFmpegError(["ffmpeg"], 127, "ffmpeg was not found on PATH")

    monkeypatch.setattr(fetch, "ffmpeg_bin", boom)
    code, out, _ = _run(["https://x.com/a/status/7"], tmp_path, quiet=True)
    assert code == 1
    assert out == (
        "FAIL https://x.com/a/status/7 ffmpeg not found; install ffmpeg or set YTP_FFMPEG\n"
    )


def test_missing_yt_dlp_is_a_clear_failure(mocked, monkeypatch, tmp_path):
    def missing():
        raise fetch.TranscribeError("yt-dlp is not installed")

    monkeypatch.setattr(fetch, "_load_yt_dlp", missing)
    code, out, _ = _run(["https://x.com/a/status/7"], tmp_path, quiet=True)
    assert code == 1
    assert out == "FAIL https://x.com/a/status/7 yt-dlp is not installed\n"


def test_duplicate_inputs_get_separate_folders(mocked, tmp_path):
    code, out, _ = _run(["https://x.com/a/status/7", "x.com/a/status/7"], tmp_path, quiet=True)
    assert code == 0
    assert (tmp_path / "out" / "x-a-7").is_dir() and (tmp_path / "out" / "x-a-7-2").is_dir()


def test_cpu_fallback_picks_small_model(mocked, monkeypatch, tmp_path):
    monkeypatch.setattr(fetch, "_cuda_or_cpu", lambda: "cpu")
    code, _out, err = _run(["https://x.com/a/status/7"], tmp_path)
    assert code == 0
    assert mocked[0].model == fetch.CPU_MODEL and mocked[0].device == "cpu"
    assert "CUDA is not available" in err  # not quiet: progress goes to stderr


def test_model_override(mocked, tmp_path):
    _run(["https://x.com/a/status/7"], tmp_path, quiet=True, model="medium.en")
    assert mocked[0].model == "medium.en"


def test_multi_video_post_takes_first_entry(mocked, monkeypatch, tmp_path):
    single = FakeYoutubeDL.extract_info

    def playlist(self, url, download=True):
        entry = single(self, url, download)
        return {"entries": [entry, None], "playlist_count": 2}

    monkeypatch.setattr(FakeYoutubeDL, "extract_info", playlist)
    code, _out, _ = _run(["https://x.com/a/status/7"], tmp_path, quiet=True)
    assert code == 0
    log = (tmp_path / "out" / "x-a-7" / fetch.LOG_NAME).read_text("utf-8")
    assert "post has 2 videos; transcribed the first" in log


# ---------------------------------------------------------------- CLI


def test_cli_from_file_and_exit_code(mocked, monkeypatch, tmp_path, capsys):
    urls = tmp_path / "urls.txt"
    urls.write_text("https://x.com/a/status/7\nhttps://x.com/a/status/404\n", "utf-8")
    code = cli.main(
        ["transcribe", "--quiet", "--from-file", str(urls), "--out", str(tmp_path / "t")]
    )
    lines = capsys.readouterr().out.splitlines()
    assert code == 1
    assert lines[0].startswith("OK ") and lines[1].startswith("FAIL ")


def test_cli_env_default_out(mocked, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("YTP_TRANSCRIPTS", str(tmp_path / "env-out"))
    assert cli.main(["transcribe", "--quiet", "https://x.com/a/status/7"]) == 0
    assert (tmp_path / "env-out" / "x-a-7" / "transcript.txt").is_file()


def test_cli_needs_input(capsys):
    assert cli.main(["transcribe"]) == 2
    assert "at least one URL" in capsys.readouterr().err


def test_default_out_folder_is_gitignored():
    ignore = (Path(__file__).resolve().parents[2] / ".gitignore").read_text("utf-8")
    assert "transcripts/" in ignore.splitlines()
