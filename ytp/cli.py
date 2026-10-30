"""Booth's command line. `python -m ytp`."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml

from ytp import __version__
from ytp.check import check_render, load_asr, write_report
from ytp.fetch import CPU_MODEL, DEFAULT_OUT, GPU_MODEL, read_url_file
from ytp.fetch import Options as TranscribeOptions
from ytp.fetch import run as run_transcribe
from ytp.finish import finish_episode
from ytp.import_ep01 import import_episode
from ytp.index import index_episode
from ytp.layout import init_episode
from ytp.plan import load_plan, save_plan, validate_plan
from ytp.render import allocate_render_path, infer_episode_dir, render_plan
from ytp.rules import SECTIONS, add_rule, load_rules, rules_path
from ytp.selftest import self_test_failed, self_test_lines
from ytp.take import ingest_take, warn_cpu
from ytp.transcribe import FasterWhisperTranscriber, NullTranscriber, _cuda_or_cpu


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ytp",
        description=(
            "Local YouTube editing pipeline for the Scott Mastin channel. "
            "Draft only: this tool never publishes and never changes a listing."
        ),
    )
    parser.add_argument("--version", action="version", version=f"ytp {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    quick = sub.add_parser(
        "transcribe",
        help="timestamped transcript for public video links (X first) or local files",
        description=(
            "Download a public post's audio with yt-dlp (or read a local file) and transcribe "
            "it with local faster-whisper. Public posts only: no cookies or browser logins."
        ),
    )
    quick.add_argument("inputs", nargs="*", help="video URLs or local media files")
    quick.add_argument(
        "--from-file",
        type=Path,
        action="append",
        default=None,
        help="text file with one URL or path per line (# comments allowed). Repeatable.",
    )
    quick.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            "folder that gets one subfolder per video, named from the post id "
            "(default: $YTP_TRANSCRIPTS or ./transcripts)"
        ),
    )
    quick.add_argument(
        "--model",
        default="auto",
        help=f"Whisper model (default auto: {GPU_MODEL} on CUDA, {CPU_MODEL} on CPU)",
    )
    quick.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    quick.add_argument(
        "--language",
        default="en",
        help="spoken language code, or auto to detect (default en)",
    )
    quick.add_argument(
        "--keep-media",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="keep the downloaded audio in the video folder (default: keep)",
    )
    quick.add_argument(
        "--vad",
        action="store_true",
        help="also run Silero VAD; cuts more noise but can drop speech under loud noise",
    )
    quick.add_argument(
        "--refresh",
        action="store_true",
        help="download again even when the video folder already has media.* from a past run",
    )
    quick.add_argument(
        "--quiet",
        action="store_true",
        help="logs go to transcribe.log; stdout is one OK/FAIL line per input",
    )
    quick.add_argument(
        "--summary-lines",
        type=int,
        default=0,
        metavar="N",
        help="print the first N transcript lines after each OK line (default 0)",
    )
    quick.set_defaults(func=_cmd_transcribe)

    init = sub.add_parser("init", help="create the standard episode folders")
    init.add_argument("episode_dir", type=Path)
    init.add_argument("--series", default="ai-survival")
    init.add_argument("--force", action="store_true", help="rewrite episode.yaml")
    init.set_defaults(func=_cmd_init)

    rule = sub.add_parser("rule", help="read or append series rules")
    rule_sub = rule.add_subparsers(dest="rule_command", required=True)

    add = rule_sub.add_parser("add", help="append one note from Scott")
    add.add_argument("--series", required=True)
    add.add_argument("--section", required=True, choices=SECTIONS)
    add.add_argument("--text", required=True)
    add.add_argument(
        "--rules-root",
        type=Path,
        default=None,
        help="directory that contains rules/ (defaults to the repo)",
    )
    add.set_defaults(func=_cmd_rule_add)

    listing = rule_sub.add_parser("list", help="print rules for a series")
    listing.add_argument("--series", required=True)
    listing.add_argument("--section", choices=SECTIONS)
    listing.add_argument("--rules-root", type=Path, default=None)
    listing.set_defaults(func=_cmd_rule_list)

    plan = sub.add_parser("plan", help="import or validate an edit plan")
    plan_sub = plan.add_subparsers(dest="plan_command", required=True)

    importer = plan_sub.add_parser(
        "import",
        help="build a plan from an Ep01 timeline, concat list, and score sheet",
    )
    importer.add_argument("--timeline", type=Path, required=True)
    importer.add_argument("--concat", type=Path, required=True)
    importer.add_argument("--score", type=Path, required=True)
    importer.add_argument("--lines", type=Path, default=None)
    importer.add_argument("--series", default="ai-survival")
    importer.add_argument("--episode", default="ep01")
    importer.add_argument("--title", default="How to Survive an AI Drone")
    importer.add_argument("--episode-dir", type=Path, default=None)
    importer.add_argument(
        "--voice",
        default=None,
        help="mix (default when audio/mix has files), voice, or a relative path",
    )
    importer.add_argument("--out", type=Path, required=True)
    importer.set_defaults(func=_cmd_plan_import)

    checker = plan_sub.add_parser("validate", help="check a plan against the schema rules")
    checker.add_argument("plan", type=Path)
    checker.set_defaults(func=_cmd_plan_validate)

    index = sub.add_parser("index", help="transcribe source clips into edit/clip-index")
    index.add_argument("episode_dir", type=Path)
    index.add_argument("--model", default="base")
    index.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    index.add_argument("--backend", default="whisper", choices=["whisper", "none"])
    index.add_argument(
        "--root",
        action="append",
        default=None,
        help="folder to scan, relative to the episode (default: source). Repeatable.",
    )
    index.set_defaults(func=_cmd_index)

    render = sub.add_parser("render", help="render an edit plan to mp4")
    render.add_argument("plan", type=Path)
    render.add_argument("--out", type=Path, default=None)
    render.add_argument("--episode-dir", type=Path, default=None)
    render.add_argument("--captions", choices=["srt", "burn", "both"])
    render.add_argument("--preset", default="veryfast")
    render.add_argument("--encoder", default="libx264", help="libx264, nvenc, or auto")
    render.add_argument("--audio-hook", type=Path, default=None, help="Track's mix script")
    render.add_argument("--graphics-hook", type=Path, default=None, help="Studio's template script")
    render.add_argument("--short", default=None, help="also write this short id from the plan")
    render.add_argument(
        "--no-master",
        action="store_true",
        help="skip loudnorm and encode the mix at its current level",
    )
    render.add_argument(
        "--denoise",
        action="store_true",
        help="highpass and light afftdn before loudnorm (off unless set)",
    )
    render.set_defaults(func=_cmd_render)

    checker = sub.add_parser("check", help="pass/fail a render against the plan and series rules")
    checker.add_argument("video", type=Path)
    checker.add_argument("--plan", type=Path, required=True)
    checker.add_argument("--series", required=True)
    checker.add_argument("--rules-root", type=Path, default=None)
    checker.add_argument(
        "--asr",
        type=Path,
        default=None,
        help="word JSON or [start-end] transcript",
    )
    checker.add_argument("--window", type=float, default=None, help="seconds each side of a splice")
    checker.add_argument("--profile", choices=["auto", "episode", "short"], default="auto")
    checker.add_argument("--out-dir", type=Path, default=None)
    checker.add_argument("--model", default="base", help="model for the whole-file transcript")
    checker.add_argument(
        "--splice-model",
        default="small",
        help="model for the ±1.5s splice windows (default small)",
    )
    checker.add_argument("--device", default="cpu", help="splice check stays on CPU")
    checker.set_defaults(func=_cmd_check)

    take = sub.add_parser("take", help="init an episode beside a camera take and transcribe it")
    take.add_argument("video", type=Path)
    take.add_argument(
        "--series",
        default="talking-head",
        choices=["talking-head", "ai-survival"],
    )
    take.add_argument(
        "--episode",
        default=None,
        help="folder name beside the take (default: the file stem)",
    )
    take.set_defaults(func=_cmd_take)

    finish = sub.add_parser("finish", help="validate, render with encoder auto, and check")
    finish.add_argument("episode", nargs="?", type=Path, default=None)
    finish.add_argument("--rules-root", type=Path, default=None)
    finish.add_argument(
        "--no-master",
        action="store_true",
        help="skip loudnorm and encode the mix at its current level",
    )
    finish.add_argument(
        "--denoise",
        action="store_true",
        help="highpass and light afftdn before loudnorm (off unless set)",
    )
    finish.set_defaults(func=_cmd_finish)

    probe = sub.add_parser(
        "self-test",
        help="print Python, ffmpeg, NVENC, CUDA, and a tiny whisper run",
    )
    probe.set_defaults(func=_cmd_self_test)
    return parser


def _cmd_init(args: argparse.Namespace) -> int:
    manifest = init_episode(args.episode_dir, args.series, force=args.force)
    print(f"episode layout ready: {args.episode_dir}")
    print(f"manifest: {manifest}")
    return 0


def _cmd_rule_add(args: argparse.Namespace) -> int:
    root = args.rules_root
    rule = add_rule(args.series, args.section, args.text, root=root)
    print(f"added {rule.id} to {rules_path(args.series, root)}")
    return 0


def _cmd_plan_import(args: argparse.Namespace) -> int:
    episode_dir = args.episode_dir
    if episode_dir is None and args.out.parent.name == "edit":
        episode_dir = args.out.parent.parent
    plan = import_episode(
        timeline_path=args.timeline,
        concat_path=args.concat,
        score_path=args.score,
        lines_path=args.lines,
        series=args.series,
        episode=args.episode,
        title=args.title,
        episode_dir=episode_dir,
        voice_preference=args.voice,
    )
    errors = validate_plan(plan)
    for error in errors:
        print(f"ytp: {error}", file=sys.stderr)
    save_plan(plan, args.out)
    print(f"wrote {args.out}")
    print(
        f"narration {len(plan.narration)}  segments {len(plan.segments)}  music {len(plan.music)}"
    )
    missing = plan.meta.get("missing_media") or []
    for relative in missing:
        print(f"warning: missing {relative}", file=sys.stderr)
    if missing:
        print(
            f"warning: {len(missing)} imported path(s) are not in the episode folder yet",
            file=sys.stderr,
        )
    return 1 if errors else 0


def _cmd_plan_validate(args: argparse.Namespace) -> int:
    errors = validate_plan(load_plan(args.plan))
    if errors:
        for error in errors:
            print(f"ytp: {error}", file=sys.stderr)
        return 1
    print(f"ok {args.plan}")
    return 0


def _series_language(series: str, root: Path | None) -> str:
    if not series:
        return "en"
    try:
        rules = load_rules(series, root)
    except FileNotFoundError:
        return "en"
    return str(rules.lint.get("language") or "en")


def _episode_series(episode_dir: Path) -> str:
    manifest = episode_dir / "episode.yaml"
    if not manifest.is_file():
        return ""
    data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    return str(data.get("series") or "")


def _cmd_index(args: argparse.Namespace) -> int:
    if args.backend == "none":
        transcriber = NullTranscriber()
    else:
        language = _series_language(_episode_series(args.episode_dir), None)
        transcriber = FasterWhisperTranscriber(args.model, args.device, language)
    roots = [Path(item) for item in args.root] if args.root else None
    document = index_episode(args.episode_dir, transcriber, roots=roots)
    clips = document["clips"]
    assert isinstance(clips, list)
    print(f"indexed {len(clips)} clip(s) -> {args.episode_dir / 'edit' / 'clip-index.json'}")
    return 0


def _cmd_render(args: argparse.Namespace) -> int:
    plan = load_plan(args.plan)
    episode_dir = args.episode_dir or infer_episode_dir(args.plan)
    if args.out:
        out = args.out
    else:
        out = allocate_render_path(episode_dir, f"{args.plan.stem}.mp4")
    if out.exists():
        print(f"ytp: refusing to overwrite {out}", file=sys.stderr)
        return 1
    result = render_plan(
        plan,
        out,
        episode_dir=episode_dir,
        plan_path=args.plan,
        captions=args.captions,
        preset=args.preset,
        encoder=args.encoder,
        audio_hook=args.audio_hook,
        graphics_hook=args.graphics_hook,
        short_id=args.short,
        master=not args.no_master,
        denoise=args.denoise,
    )
    print(f"wrote {result.video}")
    if result.srt:
        print(f"srt {result.srt}")
    if result.short:
        print(f"short {result.short}")
    for line in result.logs:
        print(line)
    for warning in result.warnings:
        print(f"warning: {warning}")
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    plan = load_plan(args.plan)
    episode_dir = infer_episode_dir(args.plan)
    asr = load_asr(args.asr) if args.asr else None
    if asr is not None:
        transcriber = None
    else:
        language = _series_language(args.series, args.rules_root)
        transcriber = FasterWhisperTranscriber(
            args.model,
            args.device,
            language,
            window_model=args.splice_model,
        )
    report = check_render(
        args.video,
        plan,
        series=args.series,
        plan_path=args.plan,
        rules_root=args.rules_root,
        transcriber=transcriber,
        asr=asr,
        window=args.window,
        profile=args.profile,
        episode_dir=episode_dir,
    )
    out_dir = args.out_dir
    if out_dir is None:
        qc = episode_dir / "qc"
        out_dir = qc if episode_dir.exists() else args.video.parent
    markdown, payload = write_report(report, out_dir)
    print("PASS" if report.passed else "FAIL")
    print(_console_text(markdown.read_text(encoding="utf-8")))
    print(payload)
    return 0 if report.passed else 1


def _cmd_rule_list(args: argparse.Namespace) -> int:
    rules = load_rules(args.series, args.rules_root)
    names = [args.section] if args.section else list(rules.sections)
    for name in names:
        owner = rules.owners.get(name, "")
        print(f"## {name} ({owner})")
        for rule in rules.sections[name]:
            print(f"{rule.id}: {rule.text}")
        print()
    return 0


def _cmd_take(args: argparse.Namespace) -> int:
    video = args.video
    if not video.is_file():
        print(f"ytp: take not found: {video}", file=sys.stderr)
        return 1
    device = _cuda_or_cpu()
    if device != "cuda":
        warn_cpu("CUDA is not available; transcribing on CPU int8")
        device = "cpu"
    transcriber = FasterWhisperTranscriber("small", device, "en")
    try:
        episode_dir = ingest_take(
            video,
            series=args.series,
            episode=args.episode,
            transcriber=transcriber,
        )
    except RuntimeError as exc:
        if device == "cuda" and "faster-whisper" not in str(exc):
            warn_cpu(f"CUDA transcription failed ({exc}); retrying on CPU int8")
            transcriber = FasterWhisperTranscriber("small", "cpu", "en")
            episode_dir = ingest_take(
                video,
                series=args.series,
                episode=args.episode,
                transcriber=transcriber,
            )
        else:
            raise
    print(f"episode: {episode_dir}")
    print(f"transcript: {episode_dir / 'edit' / 'transcript.txt'}")
    print(f"words: {episode_dir / 'edit' / 'words.json'}")
    return 0


def _cmd_finish(args: argparse.Namespace) -> int:
    episode = args.episode or Path.cwd()
    return finish_episode(
        episode,
        rules_root=args.rules_root,
        master=not args.no_master,
        denoise=args.denoise,
    )


def _cmd_transcribe(args: argparse.Namespace) -> int:
    inputs = list(args.inputs)
    for path in args.from_file or []:
        inputs.extend(read_url_file(path))
    if not inputs:
        print("ytp: transcribe needs at least one URL, file, or --from-file", file=sys.stderr)
        return 2
    if args.summary_lines < 0:
        print("ytp: --summary-lines must be 0 or more", file=sys.stderr)
        return 2
    out = args.out
    if out is None:
        env = os.environ.get("YTP_TRANSCRIPTS", "").strip().strip('"')
        out = Path(env) if env else DEFAULT_OUT
    options = TranscribeOptions(
        out=out,
        model=args.model,
        device=args.device,
        language=args.language,
        keep_media=args.keep_media,
        quiet=args.quiet,
        summary_lines=args.summary_lines,
        refresh=args.refresh,
        vad=args.vad,
    )
    return run_transcribe(inputs, options)


def _cmd_self_test(args: argparse.Namespace) -> int:
    del args
    lines = self_test_lines()
    for line in lines:
        print(line)
    return 1 if self_test_failed(lines) else 0


def _console_text(text: str) -> str:
    """PowerShell 5.1 consoles garble a UTF-8 plus-minus sign."""
    return text.replace("\u00b1", "+/-")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (FileNotFoundError, ValueError) as exc:
        print(f"ytp: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
