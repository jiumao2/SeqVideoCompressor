from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from tqdm.auto import tqdm

from .ffmpeg_tools import FFmpegRuntime, fps_rational, probe_video
from .naming import OutputPaths
from .encoding import EncodingSettings
from .runtime_utils import ProcessMonitor, atomic_save_npy
from .seq_reader import SeqReader


@dataclass(frozen=True)
class EncodeResult:
    encoding: str
    start_frame: int
    frame_count: int
    wall_seconds: float
    encoding_fps: float
    cpu_seconds: float
    peak_rss_bytes: int
    video_bytes: int
    timestamps_bytes: int
    command: list[str]
    probe: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _input_args(
    reader: SeqReader,
    runtime: FFmpegRuntime,
) -> list[str]:
    fps = fps_rational(reader.header.frame_rate)
    return [
        str(runtime.ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-framerate",
        fps,
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "-i",
        "pipe:0",
    ]


def _feed_frames(
    process: subprocess.Popen[bytes],
    reader: SeqReader,
    start: int,
    count: int,
) -> None:
    assert process.stdin is not None
    try:
        for jpeg in reader.iter_jpeg_payloads(start, count, validate=True):
            process.stdin.write(jpeg)
    finally:
        process.stdin.close()


def _follow_ffmpeg_progress(stream, progress, frame_count: int) -> None:
    """Update a tqdm-compatible progress bar from FFmpeg ``-progress`` output."""
    for raw_line in iter(stream.readline, b""):
        line = raw_line.decode("utf-8", errors="replace").strip()
        key, separator, value = line.partition("=")
        if not separator or key != "frame":
            continue
        try:
            completed = min(max(int(value.strip()), 0), frame_count)
        except ValueError:
            continue
        if completed > progress.n:
            progress.update(completed - progress.n)


def encode_segment(
    reader: SeqReader,
    runtime: FFmpegRuntime,
    settings: EncodingSettings,
    outputs: OutputPaths,
    *,
    start: int = 0,
    count: int | None = None,
    overwrite: bool = False,
    show_progress: bool = False,
) -> EncodeResult:
    start, stop = reader._normalized_range(start, count)
    frame_count = stop - start
    outputs.video.parent.mkdir(parents=True, exist_ok=True)
    for path in (outputs.video, outputs.timestamps):
        if path.exists() and not overwrite:
            raise FileExistsError(f"Refusing to overwrite {path}")
    with tempfile.NamedTemporaryFile(
        prefix=f".{outputs.video.stem}.",
        suffix=".seqcomp.tmp.mkv",
        dir=outputs.video.parent,
        delete=False,
    ) as temporary_stream:
        temporary_video = Path(temporary_stream.name)
    command = _input_args(reader, runtime)
    if show_progress:
        command += ["-progress", "pipe:1", "-nostats"]
    command += [
        "-map",
        "0:v:0",
        "-an",
        "-frames:v",
        str(frame_count),
        "-r",
        fps_rational(reader.header.frame_rate),
        *settings.ffmpeg_args(),
        "-map_metadata",
        "-1",
        "-y",
        str(temporary_video),
    ]
    progress = tqdm(
        total=frame_count,
        desc=f"Encoding {reader.seq_path.name}",
        unit="frame",
        dynamic_ncols=True,
        mininterval=0.5,
        smoothing=0.1,
        disable=not show_progress,
    )
    with tempfile.TemporaryFile(mode="w+b") as stderr_stream:
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE if show_progress else subprocess.DEVNULL,
                stderr=stderr_stream,
            )
        except BaseException:
            progress.close()
            temporary_video.unlink(missing_ok=True)
            raise
        monitor = ProcessMonitor(process.pid)
        monitor.start()
        progress_thread: threading.Thread | None = None
        if show_progress:
            assert process.stdout is not None
            progress_thread = threading.Thread(
                target=_follow_ffmpeg_progress,
                args=(process.stdout, progress, frame_count),
                daemon=True,
            )
            progress_thread.start()
        started = time.perf_counter()
        feed_error: BaseException | None = None
        try:
            _feed_frames(process, reader, start, frame_count)
            return_code = process.wait()
        except BaseException as exc:
            feed_error = exc
            process.kill()
            process.wait()
            return_code = process.returncode
        wall_seconds = time.perf_counter() - started
        if progress_thread is not None:
            progress_thread.join()
        if return_code == 0 and progress.n < frame_count:
            progress.update(frame_count - progress.n)
        progress.close()
        usage = monitor.stop()
        stderr_stream.seek(0)
        stderr_text = stderr_stream.read().decode("utf-8", errors="replace")
    if feed_error is not None:
        if temporary_video.exists():
            temporary_video.unlink()
        raise RuntimeError(f"Frame feeding failed: {feed_error}\n{stderr_text}") from feed_error
    if return_code != 0:
        if temporary_video.exists():
            temporary_video.unlink()
        raise RuntimeError(f"FFmpeg failed with code {return_code}:\n{stderr_text}")
    os.replace(temporary_video, outputs.video)
    atomic_save_npy(outputs.timestamps, reader.timestamps_us[start:stop])
    video_probe = probe_video(runtime, outputs.video)
    return EncodeResult(
        encoding=settings.name,
        start_frame=start,
        frame_count=frame_count,
        wall_seconds=wall_seconds,
        encoding_fps=frame_count / wall_seconds,
        cpu_seconds=usage.cpu_seconds,
        peak_rss_bytes=usage.peak_rss_bytes,
        video_bytes=outputs.video.stat().st_size,
        timestamps_bytes=outputs.timestamps.stat().st_size,
        command=[str(item) for item in command],
        probe=video_probe,
    )
