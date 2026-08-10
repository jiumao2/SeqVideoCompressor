from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from fractions import Fraction
from functools import lru_cache
from pathlib import Path


class FFmpegCapabilityError(RuntimeError):
    pass


@dataclass(frozen=True)
class FFmpegRuntime:
    ffmpeg: Path
    ffprobe: Path
    version: str
    encoders: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["ffmpeg"] = str(self.ffmpeg)
        data["ffprobe"] = str(self.ffprobe)
        data["encoders"] = list(self.encoders)
        return data


def _env_candidates() -> list[Path]:
    prefix = Path(sys.prefix)
    if os.name == "nt":
        return [prefix / "Library" / "bin" / "ffmpeg.exe"]
    return [prefix / "bin" / "ffmpeg"]


def find_ffmpeg(*, require_environment: bool = True) -> Path:
    override = os.environ.get("SEQCOMP_FFMPEG")
    if override:
        path = Path(override)
        if path.is_file():
            return path.resolve()
        raise FFmpegCapabilityError(f"SEQCOMP_FFMPEG does not exist: {path}")
    for candidate in _env_candidates():
        if candidate.is_file():
            return candidate.resolve()
    if require_environment:
        expected = _env_candidates()[0]
        raise FFmpegCapabilityError(
            f"Conda-environment FFmpeg not found at {expected}. "
            "Update the seqcomp environment from environment.yml."
        )
    found = shutil.which("ffmpeg")
    if not found:
        raise FFmpegCapabilityError("FFmpeg was not found")
    return Path(found).resolve()


def _capture_text(command: list[str], label: str) -> str:
    for attempt in range(3):
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        output = result.stdout or result.stderr
        if output.strip():
            return output
        if attempt < 2:
            time.sleep(0.05 * (attempt + 1))
    raise FFmpegCapabilityError(f"{label} returned no output")


@lru_cache(maxsize=2)
def inspect_ffmpeg(*, require_environment: bool = True) -> FFmpegRuntime:
    ffmpeg = find_ffmpeg(require_environment=require_environment)
    ffprobe_name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    ffprobe = ffmpeg.with_name(ffprobe_name)
    if not ffprobe.is_file():
        raise FFmpegCapabilityError(f"ffprobe not found beside FFmpeg: {ffprobe}")
    version_output = _capture_text([str(ffmpeg), "-version"], "FFmpeg version probe")
    version = version_output.splitlines()[0]
    required = ("libx264", "libx265")
    encoder_output = ""
    missing = list(required)
    for attempt in range(3):
        encoder_output = _capture_text(
            [str(ffmpeg), "-hide_banner", "-encoders"], "FFmpeg encoder probe"
        )
        missing = [name for name in required if name not in encoder_output]
        if not missing:
            break
        time.sleep(0.05 * (attempt + 1))
    if missing:
        raise FFmpegCapabilityError(
            f"FFmpeg lacks required encoders: {', '.join(missing)}"
        )
    for encoder in required:
        details = ""
        for attempt in range(3):
            details = _capture_text(
                [str(ffmpeg), "-hide_banner", "-h", f"encoder={encoder}"],
                f"{encoder} capability probe",
            )
            if "gray" in details:
                break
            time.sleep(0.05 * (attempt + 1))
        if "gray" not in details:
            raise FFmpegCapabilityError(f"{encoder} does not advertise gray input")
    return FFmpegRuntime(ffmpeg, ffprobe.resolve(), version, required)


def fps_rational(frame_rate: float) -> str:
    if frame_rate <= 0:
        raise ValueError(f"Invalid frame rate: {frame_rate}")
    fraction = Fraction(str(frame_rate)).limit_denominator(1_000_000)
    return f"{fraction.numerator}/{fraction.denominator}"


def probe_video(runtime: FFmpegRuntime, path: str | Path) -> dict[str, object]:
    command = [
        str(runtime.ffprobe),
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type,codec_name,pix_fmt,width,height,avg_frame_rate,nb_frames",
        "-of",
        "json",
        str(path),
    ]
    import json

    last_error: BaseException | None = None
    for attempt in range(3):
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        try:
            streams = json.loads(result.stdout).get("streams", [])
            videos = [
                stream
                for stream in streams
                if stream.get("codec_type") == "video"
            ]
            if videos:
                video = dict(videos[0])
                video["audio_stream_count"] = sum(
                    stream.get("codec_type") == "audio" for stream in streams
                )
                video["stream_count"] = len(streams)
                return video
            last_error = FFmpegCapabilityError(f"No video stream found in {path}")
        except json.JSONDecodeError as exc:
            last_error = exc
        if attempt < 2:
            time.sleep(0.05 * (attempt + 1))
    raise FFmpegCapabilityError(f"ffprobe returned invalid output for {path}: {last_error}")
