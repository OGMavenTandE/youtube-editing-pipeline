"""`ytp transcribe` with yt-dlp and Whisper mocked. No network, no model download."""

from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

import pytest

from ytp import cli, fetch
from ytp.transcribe import (
    FasterWhisperTranscriber,
    Transcriber,
    Transcript,
    Word,
    is_stock_hallucination,
    repeated_runs,
)


class FakeTranscriber(Transcriber):
    def __init__(
        self, model: str, device: str, language: str, *, quick: bool = False, vad: bool = False
    ) -> None:
        self.quick = quick
        self.vad = vad
        self.compute_type = "float16" if device == "cuda" else "int8"
        self.load_seconds = 0.0
        self.dropped_segments = 0
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

    def factory(model, device, language, *, quick=False, vad=False):
        made.append(FakeTranscriber(model, device, language, quick=quick, vad=vad))
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
    failed_log = (tmp_path / "out" / "x-a-404-failed" / fetch.LOG_NAME).resolve()
    assert lines[0] == (
        f"FAIL https://x.com/a/status/404 post deleted or unavailable; log {failed_log}"
    )
    folder = (tmp_path / "out" / "x-a-7").resolve()
    assert lines[1] == f"OK {folder / 'transcript.txt'} 1:23"
    assert lines[2].startswith(
        "FAIL https://x.com/a/status/401 login required; needs a public post; log "
    )
    assert lines[3].startswith("FAIL https://x.com/a/status/0 no video found")
    assert lines[4].startswith(f"FAIL {tmp_path / 'missing.mp4'} file not found; log ")
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
    assert out.startswith(
        "FAIL https://x.com/a/status/7 ffmpeg not found; install ffmpeg or set YTP_FFMPEG; log "
    )


def test_missing_yt_dlp_is_a_clear_failure(mocked, monkeypatch, tmp_path):
    def missing():
        raise fetch.TranscribeError("yt-dlp is not installed")

    monkeypatch.setattr(fetch, "_load_yt_dlp", missing)
    code, out, _ = _run(["https://x.com/a/status/7"], tmp_path, quiet=True)
    assert code == 1
    assert out.startswith("FAIL https://x.com/a/status/7 yt-dlp is not installed; log ")


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


# ---------------------------------------------------------------- polish (PC test findings)


def test_log_and_json_record_compute_type_and_load_time(mocked, tmp_path):
    code, _out, _ = _run(["https://x.com/a/status/7"], tmp_path, quiet=True)
    assert code == 0
    folder = tmp_path / "out" / "x-a-7"
    log = (folder / fetch.LOG_NAME).read_text("utf-8")
    assert re.search(
        rf"whisper model={fetch.GPU_MODEL} device=cuda compute_type=float16 load=\d+\.\ds", log
    )
    payload = json.loads((folder / "transcript.json").read_text("utf-8"))
    assert payload["compute_type"] == "float16"
    assert mocked[0].quick is True  # the hallucination filters are on for this command
    assert mocked[0].vad is False  # VAD is opt-in


def test_cli_vad_flag(mocked, tmp_path, capsys):
    out = str(tmp_path / "t")
    assert cli.main(["transcribe", "--quiet", "--vad", "--out", out, "x.com/a/status/7"]) == 0
    assert mocked[0].vad is True


def test_cpu_compute_type_is_int8(mocked, tmp_path):
    _run(["https://x.com/a/status/7"], tmp_path, quiet=True, device="cpu")
    payload = json.loads((tmp_path / "out" / "x-a-7" / "transcript.json").read_text("utf-8"))
    assert payload["compute_type"] == "int8" and payload["device"] == "cpu"


def test_second_run_reuses_media_and_metadata(mocked, tmp_path):
    url = "https://x.com/ScottTestsAI/status/7"
    assert _run([url], tmp_path, quiet=True)[0] == 0
    assert len(FakeYoutubeDL.seen_options) == 1
    code, out, _ = _run([url], tmp_path, quiet=True)
    assert code == 0 and out.startswith("OK ")
    assert len(FakeYoutubeDL.seen_options) == 1  # no second download
    folder = tmp_path / "out" / "x-ScottTestsAI-7"
    assert "reusing media.m4a" in (folder / fetch.LOG_NAME).read_text("utf-8")
    source = json.loads((folder / "source.json").read_text("utf-8"))
    assert source["title"] == "A test post" and source["uploader"] == "Scott Mastin"
    assert source["duration"] == 83.4 and source["upload_date"] == "2026-10-29"


def test_refresh_downloads_again(mocked, tmp_path):
    url = "https://x.com/a/status/7"
    _run([url], tmp_path, quiet=True)
    _run([url], tmp_path, quiet=True, refresh=True)
    assert len(FakeYoutubeDL.seen_options) == 2


def test_no_keep_media_never_reuses(mocked, tmp_path):
    folder = tmp_path / "out" / "x-a-7"
    folder.mkdir(parents=True)
    (folder / "media.m4a").write_bytes(b"old")
    _run(["https://x.com/a/status/7"], tmp_path, quiet=True, keep_media=False)
    assert len(FakeYoutubeDL.seen_options) == 1


def test_partial_download_is_not_reused(mocked, tmp_path):
    folder = tmp_path / "out" / "x-a-7"
    folder.mkdir(parents=True)
    (folder / "media.mp4.part").write_bytes(b"half")
    (folder / "media.m4a").write_bytes(b"")  # zero bytes is not a finished download
    assert fetch.existing_media(folder) is None
    _run(["https://x.com/a/status/7"], tmp_path, quiet=True)
    assert len(FakeYoutubeDL.seen_options) == 1


def test_cli_refresh_flag(mocked, tmp_path, capsys):
    out = str(tmp_path / "t")
    assert cli.main(["transcribe", "--quiet", "--out", out, "https://x.com/a/status/7"]) == 0
    assert cli.main(["transcribe", "--quiet", "--out", out, "--refresh", "x.com/a/status/7"]) == 0
    assert len(FakeYoutubeDL.seen_options) == 2


def test_failed_input_folder_gets_failed_suffix(mocked, tmp_path):
    code, out, _ = _run(["https://x.com/a/status/404"], tmp_path, quiet=True)
    assert code == 1
    assert not (tmp_path / "out" / "x-a-404").exists()
    failed = tmp_path / "out" / "x-a-404-failed"
    assert (failed / fetch.LOG_NAME).is_file()
    assert not (failed / "transcript.txt").exists()
    assert out.rstrip().endswith(str((failed / fetch.LOG_NAME).resolve()))
    # a second failure merges into the same -failed folder
    assert _run(["https://x.com/a/status/404"], tmp_path, quiet=True)[0] == 1
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["x-a-404-failed"]
    assert (failed / fetch.LOG_NAME).read_text("utf-8").count("input https://") == 2


def test_failed_rerun_keeps_earlier_good_transcript(mocked, monkeypatch, tmp_path):
    url = "https://x.com/a/status/7"
    assert _run([url], tmp_path, quiet=True)[0] == 0

    def broken(self, media):
        raise ValueError("decoder exploded")

    monkeypatch.setattr(FakeTranscriber, "transcribe", broken)
    code, out, _ = _run([url], tmp_path, quiet=True)
    assert code == 1 and out.startswith("FAIL ")
    folder = tmp_path / "out" / "x-a-7"
    assert (folder / "transcript.txt").is_file()
    assert not (tmp_path / "out" / "x-a-7-failed").exists()


@pytest.mark.parametrize(
    ("text", "start", "end", "dropped"),
    [
        (" Thank you.", 100.0, 101.5, True),
        ("Thanks for watching!", 0.0, 2.0, True),
        ("you", 5.0, 5.4, True),
        ("Thank you.", 0.0, 12.0, False),  # too long to be a stock line
        ("Thank you, Houston, we copy.", 0.0, 2.0, False),
        ("Stage 1, go.", 0.0, 1.0, False),
        ("We'll be right back.", 103.0, 106.9, True),
    ],
)
def test_stock_hallucination_lines(text, start, end, dropped):
    assert is_stock_hallucination(text, start, end) is dropped


class _Seg:
    def __init__(self, start, end, text, words=None, compression_ratio=1.2):
        self.start = start
        self.end = end
        self.text = text
        self.words = words or []
        self.compression_ratio = compression_ratio


class _Model:
    def __init__(self):
        self.kwargs = {}

    def transcribe(self, path, **kwargs):
        self.kwargs = kwargs
        segments = [
            _Seg(0.0, 3.0, " Attention all operators."),
            _Seg(40.0, 41.0, " Thank you."),
            _Seg(41.0, 44.0, " go go go go go go go go go go", compression_ratio=3.1),
            _Seg(50.0, 50.5, " Go."),
            _Seg(51.0, 51.5, " Stage two."),
            _Seg(52.0, 52.5, " Go."),
            _Seg(61.0, 62.0, " It's a good idea."),
            _Seg(62.0, 63.0, " It's a good idea."),
            _Seg(63.0, 64.0, " it's a good idea"),
            _Seg(70.0, 72.0, " Liftoff."),
        ]
        return iter(segments), type("Info", (), {"language": "en"})()


def _whisper_with(monkeypatch, quick):
    from ytp import transcribe as tr

    monkeypatch.setattr(tr, "extract_audio", lambda *a, **k: None)
    model = _Model()
    whisper = FasterWhisperTranscriber("tiny", "cpu", "en", quick=quick)
    whisper._models["tiny"] = model
    return whisper, model


def test_quick_mode_drops_hallucinations(monkeypatch, tmp_path):
    whisper, model = _whisper_with(monkeypatch, quick=True)
    transcript = whisper.transcribe(tmp_path / "a.wav")
    assert model.kwargs["vad_filter"] is False
    assert model.kwargs["hallucination_silence_threshold"] == 2.0
    assert [text for _s, _e, text in transcript.segments] == [
        "Attention all operators.",
        "Go.",
        "Stage two.",
        "Go.",
        "Liftoff.",
    ]
    assert whisper.dropped_segments == 5
    assert "idea" not in transcript.text and "Thank" not in transcript.text


def test_quick_mode_vad_is_opt_in(monkeypatch, tmp_path):
    from ytp import transcribe as tr

    monkeypatch.setattr(tr, "extract_audio", lambda *a, **k: None)
    model = _Model()
    whisper = FasterWhisperTranscriber("tiny", "cpu", "en", quick=True, vad=True)
    whisper._models["tiny"] = model
    whisper.transcribe(tmp_path / "a.wav")
    assert model.kwargs["vad_filter"] is True
    # keys that exist in both faster-whisper 1.1 and 1.2 VadOptions
    assert set(model.kwargs["vad_parameters"]) <= {"min_silence_duration_ms", "speech_pad_ms"}


def test_repeated_runs():
    texts = ["Go.", "Stage one.", "Go.", "a", "A.", "a!", "b", "b"]
    assert repeated_runs(texts) == {3, 4, 5}


def test_other_commands_keep_the_old_whisper_settings(monkeypatch, tmp_path):
    whisper, model = _whisper_with(monkeypatch, quick=False)
    transcript = whisper.transcribe(tmp_path / "a.wav")
    assert model.kwargs["vad_filter"] is False
    assert model.kwargs["hallucination_silence_threshold"] is None
    assert len(transcript.segments) == 10
