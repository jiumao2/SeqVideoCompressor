from __future__ import annotations

import json
import subprocess
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path

import numpy as np
from tqdm.auto import tqdm

from .encode import _follow_ffmpeg_progress, encode_segment
from .encoding import EncodingSettings
from .ffmpeg_tools import FFmpegRuntime, probe_video
from .naming import OutputPaths, output_paths
from .runtime_utils import (
    atomic_write_json,
    environment_details,
    sha256_file,
)
from .seq_reader import SeqReader


def _source_state(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino


def _require_unchanged_source(
    seq_path: Path,
    idx_path: Path,
    expected: tuple[tuple[int, int, int, int], tuple[int, int, int, int]],
    stage: str,
) -> None:
    current = (_source_state(seq_path), _source_state(idx_path))
    if current != expected:
        raise RuntimeError(f"SEQ/IDX source changed during {stage}")


def _count_video_packets(runtime: FFmpegRuntime, video_path: Path) -> int:
    command = [
        str(runtime.ffprobe),
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-count_packets",
        "-show_entries",
        "stream=nb_read_packets",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    try:
        return int(result.stdout.strip())
    except ValueError as exc:
        raise RuntimeError(
            f"ffprobe did not return a video packet count for {video_path}: "
            f"{result.stdout!r}"
        ) from exc


def _validate_full_decode(
    runtime: FFmpegRuntime,
    video_path: Path,
    frame_count: int,
    *,
    show_progress: bool = False,
) -> None:
    command = [
        str(runtime.ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-xerror",
        *(["-progress", "pipe:1", "-nostats"] if show_progress else []),
        "-i",
        str(video_path),
        "-map",
        "0:v:0",
        "-an",
        "-f",
        "null",
        "-",
    ]
    progress = tqdm(
        total=frame_count,
        desc=f"Validate decode {video_path.name}",
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
                stdout=subprocess.PIPE if show_progress else subprocess.DEVNULL,
                stderr=stderr_stream,
            )
        except BaseException:
            progress.close()
            raise
        try:
            if show_progress:
                assert process.stdout is not None
                _follow_ffmpeg_progress(process.stdout, progress, frame_count)
            return_code = process.wait()
        except BaseException:
            process.kill()
            process.wait()
            raise
        finally:
            if process.returncode == 0 and progress.n < frame_count:
                progress.update(frame_count - progress.n)
            progress.close()
        stderr_stream.seek(0)
        stderr_text = stderr_stream.read().decode("utf-8", errors="replace")
    if return_code != 0:
        raise RuntimeError(
            f"Full output decode failed for {video_path}: {stderr_text.strip()}"
        )


def _video_probe_error(
    settings: EncodingSettings, probe: Mapping[str, object]
) -> str | None:
    if int(probe.get("audio_stream_count", -1)) != 0:
        return "output contains an audio stream"
    if int(probe.get("stream_count", -1)) != 1:
        return "output must contain exactly one video stream"
    if probe.get("codec_name") != settings.ffprobe_codec:
        return "video codec mismatch"
    if settings.codec == "libx265" and probe.get("pix_fmt") != "gray":
        return "CPU HEVC pixel format is not gray"
    if settings.codec == "libsvtav1":
        if probe.get("profile") != "Main":
            return "SVT-AV1 profile is not Main"
        if probe.get("pix_fmt") not in {"yuv420p", "yuvj420p"}:
            return "SVT-AV1 pixel format is not 8-bit 4:2:0"
        if probe.get("color_range") != "pc":
            return "SVT-AV1 color range is not full-range (pc)"
        if probe.get("color_space") != "bt470bg":
            return "SVT-AV1 color space is not bt470bg"
    if settings.is_gpu:
        if probe.get("profile") != "Main":
            return "NVENC HEVC profile is not Main"
        if probe.get("pix_fmt") not in {"yuv420p", "yuvj420p"}:
            return "NVENC HEVC pixel format is not 8-bit 4:2:0"
        if probe.get("color_range") != "pc":
            return "NVENC HEVC color range is not full-range (pc)"
        if probe.get("color_space") != "bt470bg":
            return "NVENC HEVC color space is not bt470bg"
    return None


def compress_recording(
    seq_path: str | Path,
    output_dir: str | Path | None,
    runtime: FFmpegRuntime,
    settings: EncodingSettings,
    *,
    overwrite: bool = False,
    show_progress: bool = False,
) -> OutputPaths:
    """Create a canonical MKV, timestamp sidecar, and archival manifest."""
    input_seq_path = Path(seq_path)
    input_idx_path = Path(f"{input_seq_path}.idx")
    initial_source_state = (
        _source_state(input_seq_path),
        _source_state(input_idx_path),
    )
    if show_progress:
        print("  [read]     validating SEQ/IDX structure and JPEG samples")
    reader = SeqReader(seq_path)
    if settings.is_gpu and (
        reader.header.width % 2 != 0 or reader.header.height % 2 != 0
    ):
        raise ValueError(
            "NVIDIA HEVC Main/yuv420p requires even frame dimensions, but "
            f"{reader.header.width}x{reader.header.height} was requested. "
            "seqcomp will not pad or resize scientific video frames; omit --gpu "
            "to use native-gray CPU H.265 encoding."
        )
    jpeg_samples_checked = reader.validate_jpeg_samples(25)
    _require_unchanged_source(
        reader.seq_path,
        reader.idx_path,
        initial_source_state,
        "initial validation",
    )
    destination = Path(output_dir) if output_dir is not None else reader.seq_path.parent
    outputs = output_paths(reader.seq_path, destination)
    for path in (outputs.video, outputs.timestamps, outputs.manifest):
        if path.exists() and not overwrite:
            raise FileExistsError(f"Refusing to overwrite {path}")

    if show_progress:
        print("  [hash]     calculating source SHA-256")
    source_hashes = {
        "seq_sha256": sha256_file(
            reader.seq_path,
            show_progress=show_progress,
            description=f"Hash source {reader.seq_path.name}",
        ),
        "idx_sha256": sha256_file(
            reader.idx_path,
            show_progress=show_progress,
            description=f"Hash source {reader.idx_path.name}",
        ),
    }
    _require_unchanged_source(
        reader.seq_path, reader.idx_path, initial_source_state, "source hashing"
    )
    if show_progress:
        print("  [encode]   encoding video frames")
    encoded = encode_segment(
        reader,
        runtime,
        settings,
        outputs,
        start=0,
        count=reader.frame_count,
        overwrite=overwrite,
        show_progress=show_progress,
    )
    _require_unchanged_source(
        reader.seq_path, reader.idx_path, initial_source_state, "encoding"
    )
    probe = encoded.probe
    if show_progress:
        print("  [verify]   checking streams, packets, timestamps, and full decode")
    probe_error = _video_probe_error(settings, probe)
    if probe_error:
        raise RuntimeError(f"{probe_error}: {probe}")
    if (int(probe.get("width", -1)), int(probe.get("height", -1))) != (
        reader.header.width,
        reader.header.height,
    ):
        raise RuntimeError(
            "Output dimensions do not match the SEQ header: "
            f"{probe.get('width')}x{probe.get('height')} != "
            f"{reader.header.width}x{reader.header.height}"
        )
    packet_count = _count_video_packets(runtime, outputs.video)
    if packet_count != reader.frame_count:
        raise RuntimeError(
            f"Output packet count {packet_count} does not match "
            f"SEQ frame count {reader.frame_count}"
        )
    _validate_full_decode(
        runtime,
        outputs.video,
        reader.frame_count,
        show_progress=show_progress,
    )
    timestamps = np.load(outputs.timestamps, allow_pickle=False)
    if (
        timestamps.dtype != np.int64
        or timestamps.shape != (reader.frame_count,)
        or not np.array_equal(timestamps, reader.timestamps_us)
    ):
        raise RuntimeError("Timestamp sidecar does not exactly match the IDX")

    if show_progress:
        print("  [hash]     calculating output SHA-256")
    output_hashes = {
        "video_sha256": sha256_file(
            outputs.video,
            show_progress=show_progress,
            description=f"Hash output {outputs.video.name}",
        ),
        "timestamps_sha256": sha256_file(
            outputs.timestamps,
            show_progress=show_progress,
            description=f"Hash output {outputs.timestamps.name}",
        ),
    }
    source_package_bytes = reader.seq_path.stat().st_size + reader.idx_path.stat().st_size
    output_data_bytes = outputs.video.stat().st_size + outputs.timestamps.stat().st_size
    source_duration_seconds = reader.frame_count / reader.header.frame_rate
    manifest = {
        "schema_version": 3,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": {
            "seq_path": str(reader.seq_path.resolve()),
            "idx_path": str(reader.idx_path.resolve()),
            "seq_bytes": reader.seq_path.stat().st_size,
            "idx_bytes": reader.idx_path.stat().st_size,
            **source_hashes,
        },
        "encoding": settings.manifest_encoding(),
        "video": {
            "path": str(outputs.video.resolve()),
            "bytes": outputs.video.stat().st_size,
            "frame_count": packet_count,
            "width": reader.header.width,
            "height": reader.header.height,
            "nominal_frame_rate": reader.header.frame_rate,
            "probe": probe,
            "video_sha256": output_hashes["video_sha256"],
        },
        "timestamps": {
            "path": str(outputs.timestamps.resolve()),
            "dtype": "int64",
            "unit": "unix_us",
            "count": reader.frame_count,
            "first": int(timestamps[0]),
            "last": int(timestamps[-1]),
            "timestamps_sha256": output_hashes["timestamps_sha256"],
        },
        "compression": {
            "source_package_bytes": source_package_bytes,
            "output_video_and_timestamps_bytes": output_data_bytes,
            "ratio": source_package_bytes / output_data_bytes,
            "saved_percent": 100.0 * (1.0 - output_data_bytes / source_package_bytes),
            "source_duration_seconds": source_duration_seconds,
            "encoding_time_to_video_time": encoded.wall_seconds / source_duration_seconds,
        },
        "validation": {
            "jpeg_samples_checked": len(jpeg_samples_checked),
            "jpeg_sample_indices": jpeg_samples_checked,
            "jpeg_frames_validated_during_encoding": reader.frame_count,
            "idx_structure_valid": True,
            "timestamps_exact": True,
            "video_packet_count_exact": True,
            "full_decode_valid": True,
            "audio_stream_count": 0,
        },
        "encode": encoded.to_dict(),
        "ffmpeg": runtime.to_dict(),
        "environment": environment_details(),
        "notes": (
            "Lossy output is not pixel-exact. Acquisition timing is defined by "
            "the int64 UNIX-microsecond NPY sidecar, not MKV CFR timestamps."
        ),
    }
    atomic_write_json(outputs.manifest, manifest)
    return outputs


def verify_existing_package(
    seq_path: str | Path,
    output_dir: str | Path,
    runtime: FFmpegRuntime,
    settings: EncodingSettings,
    *,
    show_progress: bool = False,
) -> tuple[bool, str, OutputPaths]:
    """Verify that an existing lossy package is tied to this exact SEQ/IDX pair."""
    reader = SeqReader(seq_path)
    outputs = output_paths(reader.seq_path, output_dir)
    missing = [path.name for path in (outputs.video, outputs.timestamps, outputs.manifest) if not path.is_file()]
    if missing:
        return False, f"missing: {', '.join(missing)}", outputs
    try:
        manifest = json.loads(outputs.manifest.read_text(encoding="utf-8"))
        if not isinstance(manifest, Mapping):
            return False, "manifest root must be a JSON object", outputs
        required_sections = ("source", "encoding", "video", "timestamps", "validation")
        for section in required_sections:
            if not isinstance(manifest.get(section), Mapping):
                return False, f"manifest section {section!r} must be a JSON object", outputs
        schema_version = int(manifest.get("schema_version", -1))
        recorded_encoding = manifest["encoding"]
        matches, mismatch = settings.manifest_matches(
            recorded_encoding, schema_version
        )
        if not matches:
            return False, f"parameter mismatch: {mismatch}", outputs
        if show_progress:
            print("  [verify]   calculating source and package SHA-256")
        if manifest["source"].get("seq_sha256") != sha256_file(
            reader.seq_path,
            show_progress=show_progress,
            description=f"Hash source {reader.seq_path.name}",
        ):
            return False, "source SEQ SHA-256 mismatch", outputs
        if manifest["source"].get("idx_sha256") != sha256_file(
            reader.idx_path,
            show_progress=show_progress,
            description=f"Hash source {reader.idx_path.name}",
        ):
            return False, "source IDX SHA-256 mismatch", outputs
        if manifest["video"].get("video_sha256") != sha256_file(
            outputs.video,
            show_progress=show_progress,
            description=f"Hash package {outputs.video.name}",
        ):
            return False, "video SHA-256 mismatch", outputs
        if manifest["timestamps"].get("timestamps_sha256") != sha256_file(
            outputs.timestamps,
            show_progress=show_progress,
            description=f"Hash package {outputs.timestamps.name}",
        ):
            return False, "timestamp SHA-256 mismatch", outputs
        if manifest.get("validation", {}).get("full_decode_valid") is not True:
            return False, "manifest lacks a successful full-decode check", outputs
        timestamps = np.load(outputs.timestamps, allow_pickle=False)
        if timestamps.dtype != np.int64 or not np.array_equal(timestamps, reader.timestamps_us):
            return False, "timestamps do not exactly match IDX", outputs
        if show_progress:
            print("  [verify]   checking video streams, dimensions, and packet count")
        probe = probe_video(runtime, outputs.video)
        probe_error = _video_probe_error(settings, probe)
        if probe_error:
            return False, probe_error, outputs
        if (int(probe.get("width", -1)), int(probe.get("height", -1))) != (
            reader.header.width,
            reader.header.height,
        ):
            return False, "video dimensions do not match SEQ header", outputs
        if _count_video_packets(runtime, outputs.video) != reader.frame_count:
            return False, "video packet count mismatch", outputs
        _validate_full_decode(
            runtime,
            outputs.video,
            reader.frame_count,
            show_progress=show_progress,
        )
    except (
        AttributeError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
        RuntimeError,
        json.JSONDecodeError,
    ) as exc:
        return False, str(exc), outputs
    return True, "verified", outputs


def verify_standalone_package(
    outputs: OutputPaths,
    runtime: FFmpegRuntime,
    *,
    show_progress: bool = False,
) -> tuple[bool, str, Mapping[str, object] | None]:
    """Verify a completed package when its source SEQ/IDX pair is unavailable."""
    missing = [
        path.name
        for path in (outputs.video, outputs.timestamps, outputs.manifest)
        if not path.is_file()
    ]
    if missing:
        return False, f"missing: {', '.join(missing)}", None
    try:
        manifest = json.loads(outputs.manifest.read_text(encoding="utf-8"))
        if not isinstance(manifest, Mapping):
            return False, "manifest root must be a JSON object", None
        for section in ("source", "encoding", "video", "timestamps", "validation"):
            if not isinstance(manifest.get(section), Mapping):
                return False, f"manifest section {section!r} must be a JSON object", None
        video = manifest["video"]
        timestamps_record = manifest["timestamps"]
        validation = manifest["validation"]
        source = manifest["source"]
        expected_seq_name = f"{outputs.video.stem}.seq"
        identity_checks = (
            (source.get("seq_path"), expected_seq_name, "source SEQ"),
            (source.get("idx_path"), f"{expected_seq_name}.idx", "source IDX"),
            (video.get("path"), outputs.video.name, "video"),
            (
                timestamps_record.get("path"),
                outputs.timestamps.name,
                "timestamp sidecar",
            ),
        )
        for recorded_path, expected_name, label in identity_checks:
            if (
                not isinstance(recorded_path, str)
                or Path(recorded_path).name != expected_name
            ):
                return False, f"manifest {label} name mismatch", manifest
        if show_progress:
            print("  [verify]   calculating completed-package SHA-256")
        if video.get("video_sha256") != sha256_file(
            outputs.video,
            show_progress=show_progress,
            description=f"Hash package {outputs.video.name}",
        ):
            return False, "video SHA-256 mismatch", manifest
        if timestamps_record.get("timestamps_sha256") != sha256_file(
            outputs.timestamps,
            show_progress=show_progress,
            description=f"Hash package {outputs.timestamps.name}",
        ):
            return False, "timestamp SHA-256 mismatch", manifest
        validation_flags = (
            "timestamps_exact",
            "video_packet_count_exact",
            "full_decode_valid",
        )
        for flag in validation_flags:
            if validation.get(flag) is not True:
                return False, f"manifest validation flag {flag!r} is not true", manifest
        frame_count = int(video["frame_count"])
        width = int(video["width"])
        height = int(video["height"])
        timestamp_count = int(timestamps_record["count"])
        timestamps = np.load(outputs.timestamps, allow_pickle=False)
        if timestamps_record.get("dtype") != "int64":
            return False, "manifest timestamp dtype is not int64", manifest
        if timestamps_record.get("unit") != "unix_us":
            return False, "manifest timestamp unit is not unix_us", manifest
        if timestamps.dtype != np.int64 or timestamps.shape != (timestamp_count,):
            return False, "timestamp sidecar shape or dtype mismatch", manifest
        if timestamp_count != frame_count:
            return False, "timestamp count does not match video frame count", manifest
        if show_progress:
            print("  [verify]   checking streams, dimensions, packets, and full decode")
        probe = probe_video(runtime, outputs.video)
        if int(probe.get("audio_stream_count", -1)) != 0:
            return False, "output contains an audio stream", manifest
        if int(probe.get("stream_count", -1)) != 1:
            return False, "output must contain exactly one video stream", manifest
        if (int(probe.get("width", -1)), int(probe.get("height", -1))) != (
            width,
            height,
        ):
            return False, "video dimensions do not match manifest", manifest
        recorded_probe = video.get("probe")
        if isinstance(recorded_probe, Mapping):
            probe_keys = (
                "codec_name",
                "profile",
                "pix_fmt",
                "color_range",
                "color_space",
                "color_transfer",
                "color_primaries",
            )
            for key in probe_keys:
                if key in recorded_probe and probe.get(key) != recorded_probe.get(key):
                    return False, f"video {key} does not match manifest", manifest
        if _count_video_packets(runtime, outputs.video) != frame_count:
            return False, "video packet count mismatch", manifest
        _validate_full_decode(
            runtime,
            outputs.video,
            frame_count,
            show_progress=show_progress,
        )
    except (
        AttributeError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
        RuntimeError,
        json.JSONDecodeError,
    ) as exc:
        return False, str(exc), None
    return True, "verified", manifest
