"""Render an edit plan with ffmpeg.

Picture is a concat of stills and clips, then B-roll overlays. Audio is the
voice plus music cues, ducked with sidechaincompress, unless ``audio_hook``
points at Track's mix script. Captions are a sidecar SRT, a burn-in, or both.
A segment with ``template`` set is drawn by Studio's graphics hook when one
is configured. Neither hook is reimplemented here.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ytp.captions import write_srt
from ytp.ff import (
    FFmpegError,
    concat_line,
    escape_drawtext,
    escape_filter_path,
    escape_filter_text,
    run_ffmpeg,
)
from ytp.hooks import (
    audio_hook_command,
    graphics_hook_command,
    run_hook,
    write_cue_file,
)
from ytp.plan import Broll, EditPlan, MusicCue, Short
from ytp.rules import repo_root

_NVENC_OK: bool | None = None


@dataclass
class RenderResult:
    video: Path
    srt: Path | None
    short: Path | None
    warnings: list[str] = field(default_factory=list)
    commands: list[list[str]] = field(default_factory=list)


def infer_episode_dir(plan_path: Path) -> Path:
    if plan_path.parent.name == "edit":
        return plan_path.parent.parent
    return plan_path.parent


_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


def resolve_media(value: str | None, episode_dir: Path) -> Path | None:
    """Resolve a plan path against the episode directory.

    Relative paths are never looked up in the process working directory.
    Absolute paths, including Windows drive paths, are returned as given.
    """
    if not value:
        return None
    text = str(value)
    if _DRIVE.match(text) or Path(text).is_absolute():
        return Path(text)
    return episode_dir / text.replace("\\", "/")


def codec_args(encoder: str, preset: str) -> list[str]:
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "23"]
    return ["-c:v", "libx264", "-preset", preset, "-crf", "23", "-pix_fmt", "yuv420p"]


def resolve_encoder(name: str) -> str:
    if name in {"nvenc", "h264_nvenc"}:
        return "h264_nvenc"
    if name == "auto":
        return "h264_nvenc" if nvenc_works() else "libx264"
    return "libx264"


def nvenc_works() -> bool:
    """True only when a one-frame NVENC encode succeeds. Listing the encoder is not enough."""
    global _NVENC_OK
    if _NVENC_OK is not None:
        return _NVENC_OK
    try:
        run_ffmpeg(
            [
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=64x64:d=0.1",
                "-c:v",
                "h264_nvenc",
                "-f",
                "null",
                "-",
            ]
        )
        _NVENC_OK = True
    except FFmpegError:
        _NVENC_OK = False
    return _NVENC_OK


def music_filtergraph(
    *,
    cues: list[tuple[float, float | None, float]],
    duration: float,
    duck: bool,
) -> str:
    """Build the voice+music graph.

    ``cues`` is ``(at_seconds, duration_or_none, gain_db)`` in input order.
    Input 0 is the voice, already aligned to timeline zero. ``lead_in`` is not
    applied again: the voice file contains it.
    """
    voice = (
        f"[0:a]aformat=sample_rates=48000:channel_layouts=stereo,"
        f"apad,atrim=0:{duration:.3f},asetpts=PTS-STARTPTS"
    )
    if not cues:
        return voice + "[aout]"
    chains: list[str] = []
    if duck:
        chains.append(voice + ",asplit=2[voice][sc]")
    else:
        chains.append(voice + "[voice]")
    labels: list[str] = []
    for index, (at, cue_duration, gain) in enumerate(cues):
        trim = f"atrim=0:{cue_duration:.3f}," if cue_duration else ""
        delay = max(0, int(round(at * 1000)))
        label = f"m{index}"
        chains.append(
            f"[{index + 1}:a]aformat=sample_rates=48000:channel_layouts=stereo,{trim}"
            f"volume={gain:.3f}dB,adelay={delay}|{delay},apad,"
            f"atrim=0:{duration:.3f},asetpts=PTS-STARTPTS[{label}]"
        )
        labels.append(f"[{label}]")
    if len(labels) == 1:
        music = labels[0]
    else:
        chains.append(
            "".join(labels)
            + f"amix=inputs={len(labels)}:duration=longest:dropout_transition=0:normalize=0[music]"
        )
        music = "[music]"
    if duck:
        chains.append(
            f"{music}[sc]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=300[bed]"
        )
        bed = "[bed]"
    else:
        bed = music
    chains.append(f"[voice]{bed}amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]")
    return ";".join(chains)


def prepare_drawtext_font(source: Path, dest_dir: Path) -> Path:
    """Copy a bold font to a comma-free path. drawtext splits on commas."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "bold.ttf"
    shutil.copyfile(source, dest)
    if "," in dest.as_posix():
        raise RuntimeError(f"drawtext font path still contains a comma: {dest}")
    return dest


def bold_font() -> Path | None:
    candidates: list[Path] = []
    override = os.environ.get("YTP_BOLD_FONT", "").strip()
    if override:
        candidates.append(Path(override))
    candidates.append(repo_root() / "pipeline" / "fonts" / "Inter-Bold.ttf")
    candidates.append(Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"))
    candidates.append(Path(r"C:\Windows\Fonts\arialbd.ttf"))
    for path in candidates:
        if path.is_file():
            return path
    return None


def allocate_render_path(episode_dir: Path, filename: str) -> Path:
    """Next ``renders/vN/filename``. Existing version folders are left alone."""
    renders = episode_dir / "renders"
    version = 1
    while (renders / f"v{version}").exists():
        version += 1
    return renders / f"v{version}" / filename


def render_plan(
    plan: EditPlan,
    out: Path,
    *,
    episode_dir: Path,
    plan_path: Path | None = None,
    captions: str | None = None,
    preset: str = "veryfast",
    encoder: str = "libx264",
    audio_hook: Path | None = None,
    graphics_hook: Path | None = None,
    short_id: str | None = None,
) -> RenderResult:
    if not plan.segments:
        raise ValueError("plan has no picture segments")
    if out.exists():
        raise FileExistsError(f"refusing to overwrite {out}; write a new renders/vN folder")
    encoder = resolve_encoder(encoder)
    mode = captions or plan.captions_mode
    warnings: list[str] = []
    commands: list[list[str]] = []
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ytp-render-") as work_text:
        work = Path(work_text)
        picture = _picture(
            plan,
            episode_dir,
            work,
            encoder,
            preset,
            graphics_hook,
            plan_path,
            warnings,
            commands,
        )
        audio = _audio(
            plan,
            episode_dir,
            work,
            audio_hook,
            plan_path,
            warnings,
            commands,
        )
        muxed = work / "muxed.mp4"
        _mux(picture, audio, muxed, encoder, preset)
        srt_path: Path | None = None
        final = out
        if plan.captions and mode in {"srt", "both", "burn"}:
            srt_file = work / "captions.srt"
            write_srt(plan.captions, srt_file)
            if mode in {"burn", "both"}:
                burned = work / "burned.mp4"
                _burn(muxed, srt_file, burned, encoder, preset)
                muxed = burned
            if mode in {"srt", "both"}:
                srt_path = out.with_suffix(".srt")
                write_srt(plan.captions, srt_path)
        shutil.copyfile(muxed, final)
        short_path = None
        if short_id:
            short = _find_short(plan, short_id)
            short_path = out.with_name(f"{out.stem}-short-{short.id}{out.suffix}")
            if short_path.exists():
                raise FileExistsError(f"refusing to overwrite {short_path}")
            _render_short(final, short, short_path, work, encoder, preset, warnings)
    return RenderResult(
        video=final,
        srt=srt_path,
        short=short_path,
        warnings=warnings,
        commands=commands,
    )


def _find_short(plan: EditPlan, short_id: str) -> Short:
    for short in plan.shorts:
        if short.id == short_id:
            return short
    known = ", ".join(short.id for short in plan.shorts) or "(none)"
    raise ValueError(f"no short {short_id!r} in the plan (have {known})")


def _picture(
    plan: EditPlan,
    episode_dir: Path,
    work: Path,
    encoder: str,
    preset: str,
    graphics_hook: Path | None,
    plan_path: Path | None,
    warnings: list[str],
    commands: list[list[str]],
) -> Path:
    parts: list[Path] = []
    for index, segment in enumerate(plan.segments):
        source_text = segment.source
        if segment.template and graphics_hook is not None:
            drawn = work / f"template-{index:03d}.png"
            command = graphics_hook_command(
                graphics_hook,
                template=segment.template,
                out=drawn,
                width=plan.width,
                height=plan.height,
                palette=segment.palette or "",
                text=segment.template_text or "",
                segment_id=segment.id,
            )
            commands.append(command)
            run_hook(command)
            source_text = str(drawn)
        source = resolve_media(source_text, episode_dir)
        if source is None or not source.is_file():
            raise FileNotFoundError(f"segment {segment.id} source not found: {segment.source}")
        part = work / f"seg-{index:03d}.mp4"
        _encode_segment(source, part, segment, plan, encoder, preset)
        parts.append(part)
    picture = work / "picture.mp4"
    _concat(parts, picture, plan, encoder, preset)
    for index, item in enumerate(plan.broll):
        source = resolve_media(item.source, episode_dir)
        if source is None or not source.is_file():
            warnings.append(f"skipped b-roll {item.id}: {item.source} not found")
            continue
        overlaid = work / f"broll-{index:03d}.mp4"
        _overlay(picture, source, overlaid, item, plan, encoder, preset)
        picture = overlaid
    return picture


def _encode_segment(source, part, segment, plan, encoder, preset) -> None:
    scale = (
        f"scale={plan.width}:{plan.height}:force_original_aspect_ratio=decrease,"
        f"pad={plan.width}:{plan.height}:(ow-iw)/2:(oh-ih)/2:color=black,format=yuv420p"
    )
    if segment.kind == "still" or source.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
        run_ffmpeg(
            [
                "-y",
                "-loop",
                "1",
                "-framerate",
                str(plan.fps),
                "-i",
                str(source),
                "-t",
                f"{segment.duration:.3f}",
                "-vf",
                scale,
                "-r",
                str(plan.fps),
                "-fps_mode",
                "cfr",
                "-an",
                *codec_args(encoder, preset),
                str(part),
            ]
        )
        return
    args = ["-y"]
    if segment.in_point:
        args += ["-ss", f"{segment.in_point:.3f}"]
    args += ["-i", str(source)]
    args += ["-t", f"{segment.duration:.3f}", "-vf", scale, "-r", str(plan.fps), "-an"]
    args += [*codec_args(encoder, preset), str(part)]
    run_ffmpeg(args)


def _concat(parts: list[Path], dest: Path, plan: EditPlan, encoder: str, preset: str) -> None:
    listing = dest.with_suffix(".txt")
    body = "\n".join(concat_line(part) for part in parts) + "\n"
    listing.write_text(body, encoding="utf-8", newline="\n")
    try:
        run_ffmpeg(
            ["-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(dest)]
        )
        return
    except FFmpegError:
        pass
    args = ["-y"]
    for part in parts:
        args += ["-i", str(part)]
    pads = "".join(f"[{index}:v]" for index in range(len(parts)))
    graph = pads + f"concat=n={len(parts)}:v=1:a=0[v]"
    run_ffmpeg(
        [
            *args,
            "-filter_complex",
            graph,
            "-map",
            "[v]",
            "-r",
            str(plan.fps),
            "-an",
            *codec_args(encoder, preset),
            str(dest),
        ]
    )


def _overlay(base, source, dest, item: Broll, plan: EditPlan, encoder: str, preset: str) -> None:
    end = item.at + item.duration
    if item.layout == "pip-lower-right":
        pip_w = max(2, (plan.width // 3) // 2 * 2)
        scale = f"scale={pip_w}:-2"
        position = "W-w-24:H-h-24"
    else:
        scale = (
            f"scale={plan.width}:{plan.height}:force_original_aspect_ratio=decrease,"
            f"pad={plan.width}:{plan.height}:(ow-iw)/2:(oh-ih)/2:color=black"
        )
        position = "0:0"
    graph = (
        f"[1:v]setpts=PTS-STARTPTS+{item.at:.3f}/TB,{scale}[b];"
        f"[0:v][b]overlay={position}:enable='between(t,{item.at:.3f},{end:.3f})':eof_action=pass[v]"
    )
    args = ["-y", "-i", str(base)]
    if item.in_point:
        args += ["-ss", f"{item.in_point:.3f}"]
    args += ["-t", f"{item.duration:.3f}", "-i", str(source)]
    run_ffmpeg(
        [
            *args,
            "-filter_complex",
            graph,
            "-map",
            "[v]",
            "-an",
            *codec_args(encoder, preset),
            str(dest),
        ]
    )


def _audio(
    plan: EditPlan,
    episode_dir: Path,
    work: Path,
    audio_hook: Path | None,
    plan_path: Path | None,
    warnings: list[str],
    commands: list[list[str]],
) -> Path:
    duration = plan.picture_duration()
    voice = resolve_media(plan.voice, episode_dir)
    voice_path = work / "voice.wav"
    if voice is not None and voice.is_file():
        if voice.suffix.lower() == ".wav":
            shutil.copyfile(voice, voice_path)
        else:
            run_ffmpeg(["-y", "-i", str(voice), "-c:a", "pcm_s16le", str(voice_path)])
    else:
        warnings.append("no voice file; muxing silence")
        run_ffmpeg(
            [
                "-y",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=48000",
                "-t",
                f"{duration:.3f}",
                "-c:a",
                "pcm_s16le",
                str(voice_path),
            ]
        )
    mixed = work / "mix.wav"
    hook = audio_hook or (Path(plan.audio_hook) if plan.audio_hook else None)
    if hook is not None:
        cues = work / "cues.json"
        write_cue_file(plan, cues)
        plan_file = plan_path or (work / "plan-path-missing.yaml")
        command = audio_hook_command(hook, voice=voice_path, cues=cues, out=mixed, plan=plan_file)
        commands.append(command)
        run_hook(command)
        if not mixed.is_file():
            raise RuntimeError(f"audio hook did not write {mixed}")
        return mixed
    active: list[tuple[Path, MusicCue]] = []
    for cue in plan.music:
        source = resolve_media(cue.source, episode_dir)
        if source is None or not source.is_file():
            if cue.source:
                warnings.append(f"skipped music {cue.id}: {cue.source} not found")
            continue
        active.append((source, cue))
    if not active:
        run_ffmpeg(
            [
                "-y",
                "-i",
                str(voice_path),
                "-af",
                f"aformat=sample_rates=48000:channel_layouts=stereo,apad,atrim=0:{duration:.3f}",
                "-c:a",
                "pcm_s16le",
                str(mixed),
            ]
        )
        return mixed
    graph = music_filtergraph(
        cues=[(cue.at, cue.duration, cue.gain_db) for _, cue in active],
        duration=duration,
        duck=any(cue.duck_under_voice for _, cue in active),
    )
    args = ["-y", "-i", str(voice_path)]
    for source, _cue in active:
        args += ["-i", str(source)]
    run_ffmpeg(
        [*args, "-filter_complex", graph, "-map", "[aout]", "-c:a", "pcm_s16le", str(mixed)]
    )
    return mixed


def _mux(picture: Path, audio: Path, dest: Path, encoder: str, preset: str) -> None:
    run_ffmpeg(
        [
            "-y",
            "-i",
            str(picture),
            "-i",
            str(audio),
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            str(dest),
        ]
    )


def _burn(video: Path, srt: Path, dest: Path, encoder: str, preset: str) -> None:
    escaped = escape_filter_path(srt)
    run_ffmpeg(
        [
            "-y",
            "-i",
            str(video),
            "-vf",
            f"subtitles='{escaped}'",
            *codec_args(encoder, preset),
            "-c:a",
            "copy",
            str(dest),
        ]
    )


def _render_short(
    master: Path,
    short: Short,
    dest: Path,
    work: Path,
    encoder: str,
    preset: str,
    warnings: list[str],
) -> None:
    filters = [
        f"scale={short.width}:{short.height}:force_original_aspect_ratio=increase",
        f"crop={short.width}:{short.height}",
    ]
    font = bold_font()
    if short.title and font is not None:
        font_path = prepare_drawtext_font(font, work)
        filters.append(
            "drawtext=fontfile='"
            + escape_filter_text(font_path.as_posix())
            + "':text='"
            + escape_drawtext(short.title)
            + "':fontsize=64:fontcolor=white:x=(w-text_w)/2:y=80"
        )
    elif short.title:
        warnings.append("short title skipped; no bold font found")
    run_ffmpeg(
        [
            "-y",
            "-ss",
            f"{short.start:.3f}",
            "-to",
            f"{short.end:.3f}",
            "-i",
            str(master),
            "-vf",
            ",".join(filters),
            *codec_args(encoder, preset),
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(dest),
        ]
    )

