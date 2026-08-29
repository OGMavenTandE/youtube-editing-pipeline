#!/usr/bin/env python3
"""CLI entry for the local YouTube editing pipeline."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pipeline.broll.slides import PlaywrightNotFoundError, render_slides
from pipeline.config import FFmpegNotFoundError, Settings, load_settings, require_ffmpeg
from pipeline.gemini_director import GeminiConfigError, analyze_video, load_edit_script
from pipeline.media import MediaError, probe_duration, write_json
from pipeline.models import EditScript, SilenceTrimResult
from pipeline.pacing import enforce_pacing, evaluate_pacing
from pipeline.silence_remover import remove_silence
from pipeline.studio import write_studio_package


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description=(
            "Local YouTube editing pipeline: silence trim, Gemini edit script, "
            "MoviePy composite, then a YouTube Studio paste folder."
        ),
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Path to the raw talking-head video (e.g. raw_video.mp4).",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Final MP4 path. Defaults to output/<stem>_final.mp4.",
    )
    parser.add_argument(
        "--skip-silence",
        action="store_true",
        help="Skip dead-air trimming and use the input as the working cut.",
    )
    parser.add_argument(
        "--skip-gemini",
        action="store_true",
        help="Skip Gemini and composite with an empty (or --edit-script) plan.",
    )
    parser.add_argument(
        "--edit-script",
        default=None,
        help="Load a previously saved edit-script JSON instead of calling Gemini.",
    )
    parser.add_argument(
        "--transcript",
        default=None,
        help=(
            "Reuse a saved *_transcript.json or a plain-text transcript. "
            "Skips the Gemini audio transcription pass."
        ),
    )
    parser.add_argument(
        "--broll-dir",
        default=None,
        help="Directory of local B-roll files matched against cue queries.",
    )
    parser.add_argument(
        "--auto-editor",
        action="store_true",
        help=(
            "Optional tighter silence pass via auto-editor. Default is pydub + "
            "ffmpeg (pauses > 0.7s only)."
        ),
    )
    parser.add_argument(
        "--skip-slides",
        action="store_true",
        help="Skip Playwright slide generation (compositor will not have slide PNGs).",
    )
    parser.add_argument(
        "--skip-studio",
        action="store_true",
        help="Skip the YouTube Studio paste folder (video, titles, description, tags, thumbnail).",
    )
    parser.add_argument(
        "--title-index",
        type=int,
        default=0,
        choices=range(5),
        metavar="N",
        help="Which of the five titles to paste and put on the thumbnail (0-4, default 0).",
    )
    return parser


def resolve_input(raw: str, settings: Settings) -> Path:
    path = Path(raw)
    if path.is_file():
        return path.resolve()
    fallback = settings.input_dir / raw
    if fallback.is_file():
        return fallback.resolve()
    raise FileNotFoundError(
        f"Input video not found: {raw}\n"
        f"Looked at {path.resolve()} and {fallback.resolve()}."
    )


def run_pipeline(args: argparse.Namespace, settings: Settings) -> Path:
    settings.ensure_dirs()
    require_ffmpeg(settings)

    input_path = resolve_input(args.input, settings)
    output_path = (
        Path(args.output).resolve()
        if args.output
        else (settings.output_dir / f"{input_path.stem}_final.mp4").resolve()
    )

    if args.skip_silence:
        duration = probe_duration(input_path, settings)
        trim = SilenceTrimResult.passthrough(input_path, duration)
        print(f"[1/5] Silence trim skipped ({duration:.2f}s).")
    else:
        print("[1/5] Trimming silence / dead air...")
        trim = remove_silence(
            input_path,
            settings,
            use_auto_editor=args.auto_editor,
        )
        print(
            f"      backend={trim.backend}  "
            f"{trim.cut_map.original_duration:.2f}s -> "
            f"{trim.cut_map.trimmed_duration:.2f}s  "
            f"kept {len(trim.cut_map.kept_ranges)} range(s)"
        )
        write_json(
            settings.output_dir / f"{input_path.stem}_cut_map.json",
            trim.cut_map.model_dump(),
        )

    if args.edit_script:
        script = load_edit_script(Path(args.edit_script))
        print(f"[2/5] Loaded edit script from {args.edit_script}.")
    elif args.skip_gemini:
        script = EditScript.empty()
        print("[2/5] Gemini skipped (empty edit script).")
    else:
        print(f"[2/5] Asking {settings.gemini_model} for a transcript, then a scene plan...")
        transcript_path = Path(args.transcript).expanduser() if args.transcript else None
        transcript_out = settings.output_dir / f"{input_path.stem}_transcript.json"
        script = analyze_video(
            trim.output_path,
            settings,
            duration=trim.cut_map.trimmed_duration,
            transcript_path=transcript_path,
            transcript_out=transcript_out,
        )
        print(
            f"      scenes={len(script.scenes)}  "
            f"titles={len(script.metadata.titles)}  "
            f"chapters={len(script.metadata.chapters)}  "
            f"transcript={transcript_out.name}"
        )

    script = enforce_pacing(script, trim.cut_map.trimmed_duration, settings)
    report = evaluate_pacing(script, trim.cut_map.trimmed_duration, settings)
    print(
        f"      pacing scenes={report.scene_count} "
        f"(band {report.expected_min_scenes}-{report.expected_max_scenes} "
        f"for {report.duration:.0f}s)  micro-resets={report.micro_event_count}"
    )
    for warning in report.warnings:
        print(f"      pacing note: {warning}")

    if args.skip_slides:
        print("[3/5] Slides skipped.")
    else:
        print("[3/5] Rendering 1920x1080 slides...")
        assets = render_slides(script, settings)
        print(f"      slides={len(assets)}  dir={settings.slides_dir}")

    script_path = settings.output_dir / f"{input_path.stem}_edit_script.json"
    write_json(script_path, script.model_dump())
    meta_path = settings.output_dir / f"{input_path.stem}_youtube_metadata.json"
    write_json(meta_path, script.metadata.model_dump())

    layout_counts = {}
    for scene in script.scenes:
        key = scene.layout.value
        layout_counts[key] = layout_counts.get(key, 0) + 1
    layout_note = " ".join(f"{name}={count}" for name, count in layout_counts.items())
    print(f"[4/5] Compositing {settings.output_width}x{settings.output_height} ({layout_note or 'FULL_FRAME'})...")
    from pipeline.compositor import render_video

    final_path = render_video(
        trim.output_path,
        script,
        output_path,
        settings,
        broll_dir=Path(args.broll_dir) if args.broll_dir else None,
    )
    print(f"Done. Video: {final_path}")
    print(f"      Edit script: {script_path}")
    print(f"      Pipeline metadata: {meta_path}")
    if settings.slides_dir.is_dir() and any(settings.slides_dir.glob("*.png")):
        print(f"      Slides: {settings.slides_dir}")
    transcript_note = settings.output_dir / f"{input_path.stem}_transcript.json"
    if transcript_note.is_file():
        print(f"      Transcript: {transcript_note}")

    if getattr(args, "skip_studio", False):
        print("[5/5] Studio package skipped.")
        return final_path

    print("[5/5] Writing YouTube Studio folder...")
    package = write_studio_package(
        video_path=final_path,
        webcam_path=trim.output_path,
        metadata=script.metadata,
        dest_dir=settings.output_dir / f"{input_path.stem}_studio",
        settings=settings,
        fallback_title=input_path.stem.replace("_", " ").replace("-", " ").strip(),
        title_index=getattr(args, "title_index", 0),
    )
    print(f"      Studio folder: {package.directory}")
    print(f"      Paste title [{package.title_index}]: {package.paste_title}")
    return final_path


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = load_settings()
    try:
        run_pipeline(args, settings)
    except FFmpegNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except GeminiConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except PlaywrightNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 4
    except (FileNotFoundError, MediaError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
