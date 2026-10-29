"""Two-pass loudnorm on the mixed program.

Pass 1 measures with print_format=json. Pass 2 applies linear=true using the
measured I, true peak, LRA, threshold, and offset. Targets come from the
series loudness rules. Denoise, when asked, sits in front of loudnorm on
both passes so the measurement matches the signal being normalized.

Call this on the mixed wav, after music ducking. The end-card tail is already
in that mix. A dynamic fallback is kept. A failed or non-finite measure
returns the mix unchanged.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from ytp.ff import FFmpegError, probe_audio_rate, run_ffmpeg
from ytp.rules import DEFAULT_LINT, load_rules

_LOUDNORM_JSON = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.S)
_MEASURED_KEYS = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
_DEFAULT_RATE = 48000


@dataclass
class MasterResult:
    path: Path
    mastered: bool
    dynamic: bool
    denoise: bool = False
    input_i: float | None = None
    input_tp: float | None = None
    output_i: float | None = None
    output_tp: float | None = None
    normalization_type: str | None = None
    dynamic_note: str | None = None
    warnings: list[str] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    commands: list[list[str]] = field(default_factory=list)

    def as_sidecar(self) -> dict[str, object] | None:
        if not self.mastered or self.input_i is None or self.output_i is None:
            return None
        return {
            "mastered": True,
            "input_i": self.input_i,
            "input_tp": self.input_tp,
            "output_i": self.output_i,
            "output_tp": self.output_tp,
            "normalization_type": self.normalization_type,
            "denoise": self.denoise,
            "dynamic_note": self.dynamic_note,
        }


def loudness_settings(series: str, rules_root: Path | None = None) -> dict[str, object]:
    """Series loudness block, with defaults filling any key the file omits."""
    merged = dict(DEFAULT_LINT["loudness"])
    try:
        loaded = load_rules(series, rules_root).lint.get("loudness") or {}
    except (FileNotFoundError, ValueError, OSError):
        loaded = {}
    if isinstance(loaded, dict):
        merged.update(loaded)
    return merged


def parse_loudnorm(text: str) -> dict[str, str]:
    """Pull the flat loudnorm JSON object out of an ffmpeg stderr log."""
    match = _LOUDNORM_JSON.search(text)
    if match is None:
        raise ValueError("loudnorm printed no measurement")
    data = json.loads(match.group(0))
    if not isinstance(data, dict) or "input_i" not in data:
        raise ValueError("loudnorm JSON has no input_i")
    return {str(key): str(value) for key, value in data.items()}


def master_true_peak(loudness: dict[str, object]) -> float:
    """Loudnorm true-peak target. The check limit stays ``true_peak_dbtp``.

    AAC adds a few hundredths of a dB after the wav is encoded, so the master
    aims ``master_tp_margin_db`` under the limit.
    """
    limit = float(loudness.get("true_peak_dbtp", -1.5))
    margin = float(loudness.get("master_tp_margin_db", 0.5))
    return limit - margin


def dynamic_note(input_i: float, input_tp: float, target_i: float) -> str:
    """Linear gain to the loudness target, and where that would put the peak.

    A quiet desk take needs several dB of gain. That gain would push an
    already-hot peak over the limit, so dynamic mode is the normal result.
    """
    peak = input_tp + (target_i - input_i)
    return (
        "loudnorm used dynamic mode: not enough peak headroom for linear gain "
        f"(would peak at {peak:+.1f} dBTP)"
    )


def note_from_sidecar(data: dict[str, object], target_i: float) -> str | None:
    stored = data.get("dynamic_note")
    if isinstance(stored, str) and stored.strip():
        return stored
    if data.get("normalization_type") != "dynamic":
        return None
    try:
        return dynamic_note(float(data["input_i"]), float(data["input_tp"]), target_i)
    except (KeyError, TypeError, ValueError):
        return None


def measure_loudnorm_filter(*, target_i: float, target_tp: float, target_lra: float) -> str:
    return (
        f"loudnorm=I={_num(target_i)}:TP={_num(target_tp)}:LRA={_num(target_lra)}:"
        "print_format=json"
    )


def linear_loudnorm_filter(
    measured: dict[str, str],
    *,
    target_i: float,
    target_tp: float,
    target_lra: float,
) -> str:
    """Second pass. ``offset`` is the JSON field ``target_offset``."""
    return (
        f"loudnorm=I={_num(target_i)}:TP={_num(target_tp)}:LRA={_num(target_lra)}:"
        f"measured_I={measured['input_i']}:measured_TP={measured['input_tp']}:"
        f"measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}:"
        f"offset={measured['target_offset']}:linear=true:print_format=json"
    )


def denoise_filter(loudness: dict[str, object]) -> str:
    highpass = _num(float(loudness.get("highpass_hz", 80)))
    nr = _num(float(loudness.get("denoise_nr", 10)))
    nf = _num(float(loudness.get("denoise_nf", -50)))
    tn = _num(float(loudness.get("denoise_tn", 1)))
    return f"highpass=f={highpass},afftdn=nr={nr}:nf={nf}:tn={tn}"


def format_loudness_line(
    input_i: float,
    input_tp: float,
    output_i: float,
    output_tp: float,
) -> str:
    return (
        f"loudness: input I={input_i:.2f} LUFS TP={input_tp:.2f} dBTP; "
        f"output I={output_i:.2f} LUFS TP={output_tp:.2f} dBTP"
    )


def master_report_line(data: dict[str, object]) -> str | None:
    """One line for the check report's loudness section."""
    try:
        return (
            f"mastered from input {float(data['input_i']):.2f} LUFS / "
            f"{float(data['input_tp']):.2f} dBTP to output "
            f"{float(data['output_i']):.2f} LUFS / {float(data['output_tp']):.2f} dBTP"
        )
    except (KeyError, TypeError, ValueError):
        return None


def master_audio(
    source: Path,
    dest: Path,
    *,
    loudness: dict[str, object],
    denoise: bool = False,
    sample_rate: int | None = None,
) -> MasterResult:
    target_i = float(loudness.get("voice_lufs", -16))
    target_tp = master_true_peak(loudness)
    target_lra = float(loudness.get("lra", 11))
    rate = _output_rate(source, sample_rate)
    result = MasterResult(path=source, mastered=False, dynamic=False, denoise=denoise)
    measure = _with_denoise(
        measure_loudnorm_filter(target_i=target_i, target_tp=target_tp, target_lra=target_lra),
        loudness,
        denoise,
    )
    result.logs.append(f"af measure: {measure}")
    measure_cmd = ["-i", str(source), "-af", measure, "-f", "null", "-"]
    result.commands.append(measure_cmd)
    try:
        measured = parse_loudnorm(run_ffmpeg(measure_cmd).stderr or "")
    except (FFmpegError, ValueError, json.JSONDecodeError) as exc:
        result.warnings.append(_skip(f"loudnorm measurement failed ({_brief(exc)})"))
        return result
    missing = [key for key in _MEASURED_KEYS if not _finite(measured.get(key))]
    if missing:
        joined = ", ".join(missing)
        result.warnings.append(_skip(f"loudnorm measurement was not finite ({joined})"))
        return result
    apply = _with_denoise(
        linear_loudnorm_filter(
            measured,
            target_i=target_i,
            target_tp=target_tp,
            target_lra=target_lra,
        ),
        loudness,
        denoise,
    )
    result.logs.append(f"af apply: {apply}")
    apply_cmd = [
        "-y",
        "-i",
        str(source),
        "-af",
        apply,
        "-ar",
        str(rate),
        "-c:a",
        "pcm_s16le",
        str(dest),
    ]
    result.commands.append(apply_cmd)
    try:
        applied = parse_loudnorm(run_ffmpeg(apply_cmd).stderr or "")
    except (FFmpegError, ValueError, json.JSONDecodeError) as exc:
        result.warnings.append(_skip(f"loudnorm apply failed ({_brief(exc)})"))
        if dest.exists():
            dest.unlink()
        return result
    if not dest.is_file():
        result.warnings.append(_skip("loudnorm apply did not write a wav"))
        return result
    output_i = applied.get("output_i")
    output_tp = applied.get("output_tp")
    if not _finite(output_i) or not _finite(output_tp):
        result.warnings.append(_skip("loudnorm apply printed no finite output"))
        return result
    result.path = dest
    result.mastered = True
    result.normalization_type = applied.get("normalization_type") or None
    result.dynamic = result.normalization_type == "dynamic"
    result.input_i = float(measured["input_i"])
    result.input_tp = float(measured["input_tp"])
    result.output_i = float(output_i)
    result.output_tp = float(output_tp)
    result.logs.append(
        format_loudness_line(result.input_i, result.input_tp, result.output_i, result.output_tp)
    )
    if result.dynamic:
        result.dynamic_note = dynamic_note(result.input_i, result.input_tp, target_i)
        result.logs.append(result.dynamic_note)
    return result


def _output_rate(source: Path, sample_rate: int | None) -> int:
    if sample_rate is not None and sample_rate > 0:
        return int(sample_rate)
    try:
        rate = probe_audio_rate(source)
    except (FFmpegError, ValueError):
        return _DEFAULT_RATE
    return rate if rate > 0 else _DEFAULT_RATE


def _with_denoise(loudnorm: str, loudness: dict[str, object], denoise: bool) -> str:
    if not denoise:
        return loudnorm
    return f"{denoise_filter(loudness)},{loudnorm}"


def _num(value: float) -> str:
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.10g}"


def _finite(value: str | None) -> bool:
    if value is None:
        return False
    try:
        number = float(value)
    except ValueError:
        return False
    return math.isfinite(number)


def _brief(exc: BaseException) -> str:
    return str(exc).splitlines()[0][:200]


def _skip(reason: str) -> str:
    return f"{reason}; left the mix unmastered"
