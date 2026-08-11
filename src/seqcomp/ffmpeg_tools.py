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

from .encoding import EncodingSettings


class FFmpegCapabilityError(RuntimeError):
    pass


class GPUCapabilityError(FFmpegCapabilityError):
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


@lru_cache(maxsize=8)
def inspect_ffmpeg(
    *,
    require_environment: bool = True,
    required_encoders: tuple[str, ...] = ("libx264", "libx265"),
) -> FFmpegRuntime:
    ffmpeg = find_ffmpeg(require_environment=require_environment)
    ffprobe_name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    ffprobe = ffmpeg.with_name(ffprobe_name)
    if not ffprobe.is_file():
        raise FFmpegCapabilityError(f"ffprobe not found beside FFmpeg: {ffprobe}")
    version_output = _capture_text([str(ffmpeg), "-version"], "FFmpeg version probe")
    version = version_output.splitlines()[0]
    decoder_output = _capture_text(
        [str(ffmpeg), "-hide_banner", "-decoders"], "FFmpeg decoder probe"
    )
    if "mjpeg" not in decoder_output:
        raise FFmpegCapabilityError("FFmpeg lacks the required MJPEG decoder")
    encoder_output = ""
    missing = list(required_encoders)
    for attempt in range(3):
        encoder_output = _capture_text(
            [str(ffmpeg), "-hide_banner", "-encoders"], "FFmpeg encoder probe"
        )
        missing = [name for name in required_encoders if name not in encoder_output]
        if not missing:
            break
        time.sleep(0.05 * (attempt + 1))
    if missing:
        raise FFmpegCapabilityError(
            f"FFmpeg lacks required encoders: {', '.join(missing)}"
        )
    for encoder in required_encoders:
        details = _capture_text(
            [str(ffmpeg), "-hide_banner", "-h", f"encoder={encoder}"],
            f"{encoder} capability probe",
        )
        if encoder == "hevc_nvenc":
            required_text = ("yuv420p", "-cq", "-gpu")
        elif encoder == "libsvtav1":
            required_text = ("yuv420p", "-crf", "-preset")
        else:
            required_text = ("gray",)
        absent = [item for item in required_text if item not in details]
        if absent:
            raise FFmpegCapabilityError(
                f"{encoder} lacks required capabilities: {', '.join(absent)}"
            )
    return FFmpegRuntime(ffmpeg, ffprobe.resolve(), version, required_encoders)


def _nvidia_diagnostics() -> str:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return "nvidia-smi was not found"
    command = [
        executable,
        "--query-gpu=index,name,driver_version",
        "--format=csv,noheader",
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"nvidia-smi failed: {exc}"
    output = (result.stdout or result.stderr).strip()
    return output or f"nvidia-smi exited with code {result.returncode} without output"


def _gpu_failure_reason(stderr: str) -> str:
    lowered = stderr.lower()
    if "required nvenc api version" in lowered or "driver does not support" in lowered:
        return "The NVIDIA driver is too old for this FFmpeg/NVENC API."
    if "cannot load nvcuda" in lowered or "cannot load libcuda" in lowered:
        return "The NVIDIA display driver/CUDA driver library is unavailable."
    if "no capable devices found" in lowered or "unsupported device" in lowered:
        return "No NVENC-capable NVIDIA GPU is available to FFmpeg."
    if "invalid device" in lowered or "invalid device ordinal" in lowered:
        return "The requested zero-based GPU device index is invalid."
    if "out of memory" in lowered or "openencodesessionex failed" in lowered:
        return "NVENC could not open an encoding session; GPU resources may be exhausted."
    return "FFmpeg could not initialize the requested NVIDIA NVENC encoder."


def _gpu_error(
    runtime: FFmpegRuntime | None,
    settings: EncodingSettings,
    stderr: str,
    reason: str | None = None,
) -> GPUCapabilityError:
    device = "auto" if settings.gpu_device is None else str(settings.gpu_device)
    ffmpeg_details = (
        f"{runtime.ffmpeg} ({runtime.version})" if runtime is not None else "unavailable"
    )
    diagnostic = _nvidia_diagnostics()
    explanation = reason or _gpu_failure_reason(stderr)
    if settings.gpu_device is not None:
        available_indices = {
            int(line.split(",", 1)[0].strip())
            for line in diagnostic.splitlines()
            if line.split(",", 1)[0].strip().isdigit()
        }
        if available_indices and settings.gpu_device not in available_indices:
            available = ", ".join(str(index) for index in sorted(available_indices))
            explanation = (
                f"GPU device index {settings.gpu_device} is invalid; "
                f"nvidia-smi reports available index(es): {available}."
            )
    details = stderr.strip() or "FFmpeg returned no diagnostic text."
    return GPUCapabilityError(
        "NVIDIA H.265 NVENC is unavailable.\n"
        f"Reason: {explanation}\n"
        f"Requested: hevc_nvenc, device={device}, CQ={settings.cq}, "
        f"preset={settings.preset}, Main/yuv420p.\n"
        f"FFmpeg: {ffmpeg_details}\n"
        f"NVIDIA devices: {diagnostic}\n"
        f"FFmpeg diagnostic:\n{details}\n"
        "Check that this computer has an NVENC-capable NVIDIA GPU, update the "
        "NVIDIA driver, verify --gpu-device, and close other GPU encoders before "
        "retrying. Omit --gpu to use CPU encoding; seqcomp will not fall back "
        "automatically."
    )


@lru_cache(maxsize=16)
def inspect_nvenc(
    settings: EncodingSettings, *, require_environment: bool = True
) -> FFmpegRuntime:
    if not settings.is_gpu:
        raise ValueError("inspect_nvenc requires GPU encoding settings")
    runtime: FFmpegRuntime | None = None
    try:
        runtime = inspect_ffmpeg(
            require_environment=require_environment,
            required_encoders=(),
        )
        runtime = inspect_ffmpeg(
            require_environment=require_environment,
            required_encoders=("hevc_nvenc",),
        )
    except (FFmpegCapabilityError, OSError, subprocess.SubprocessError) as exc:
        raise _gpu_error(
            runtime,
            settings,
            str(exc),
            "The environment-local FFmpeg does not provide the required hevc_nvenc encoder.",
        ) from exc
    command = [
        str(runtime.ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "nullsrc=size=256x256:rate=1,format=gray",
        "-map",
        "0:v:0",
        "-an",
        "-frames:v",
        "1",
        *settings.ffmpeg_args(),
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _gpu_error(runtime, settings, str(exc)) from exc
    if result.returncode != 0:
        raise _gpu_error(runtime, settings, result.stderr or result.stdout)
    return runtime


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
        (
            "stream=codec_type,codec_name,profile,pix_fmt,width,height,avg_frame_rate,"
            "nb_frames,color_range,color_space,color_transfer,color_primaries"
        ),
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
                stream for stream in streams if stream.get("codec_type") == "video"
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
    raise FFmpegCapabilityError(
        f"ffprobe returned invalid output for {path}: {last_error}"
    )
