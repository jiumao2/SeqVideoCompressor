from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from seqcomp.compress import compress_recording, verify_existing_package
from seqcomp.encode import encode_segment
from seqcomp.encoding import DEFAULT_SETTINGS, make_settings
from seqcomp.ffmpeg_tools import inspect_ffmpeg
from seqcomp.naming import output_paths
from seqcomp.seq_reader import SeqFormatError, SeqReader


@pytest.mark.integration
@pytest.mark.parametrize("codec", ["h264", "h265"])
def test_lossy_codecs_support_odd_gray_frames_without_audio(
    tiny_seq: Path, tmp_path: Path, codec: str
) -> None:
    runtime = inspect_ffmpeg(require_environment=True)
    reader = SeqReader(tiny_seq)
    settings = make_settings(codec=codec)
    outputs = output_paths(tiny_seq, tmp_path)
    result = encode_segment(reader, runtime, settings, outputs, pipeline="jpeg-pipe")
    assert result.frame_count == reader.frame_count
    assert result.probe["audio_stream_count"] == 0
    assert result.probe["stream_count"] == 1
    assert outputs.video.is_file()
    assert np.array_equal(np.load(outputs.timestamps), reader.timestamps_us)


@pytest.mark.integration
@pytest.mark.parametrize("pipeline", ["jpeg-pipe", "opencv-raw", "direct-seq"])
def test_all_input_pipelines_create_decodable_h265(
    tiny_seq: Path, tmp_path: Path, pipeline: str
) -> None:
    runtime = inspect_ffmpeg(require_environment=True)
    reader = SeqReader(tiny_seq)
    outputs = output_paths(tiny_seq, tmp_path)
    result = encode_segment(
        reader, runtime, DEFAULT_SETTINGS, outputs, pipeline=pipeline
    )
    assert result.frame_count == reader.frame_count
    assert result.probe["codec_name"] == "hevc"
    assert result.probe["audio_stream_count"] == 0


@pytest.mark.integration
def test_default_compress_package_is_gray_hevc_without_audio(
    tiny_seq: Path, tmp_path: Path
) -> None:
    runtime = inspect_ffmpeg(require_environment=True)
    assert runtime.encoders == ("libx264", "libx265")
    outputs = compress_recording(
        tiny_seq,
        tmp_path,
        runtime,
        DEFAULT_SETTINGS,
    )
    manifest = json.loads(outputs.manifest.read_text(encoding="utf-8"))
    assert outputs.video.name == "test-recording.000.mkv"
    assert outputs.timestamps.name == "test-recording.000.timestamps.npy"
    assert manifest["encoding"] == {
        "codec": "libx265",
        "preset": "medium",
        "crf": 18,
        "keyint": 250,
        "container": "matroska",
        "audio": "none",
    }
    assert manifest["video"]["probe"]["codec_name"] == "hevc"
    assert manifest["video"]["probe"]["pix_fmt"] == "gray"
    assert manifest["video"]["probe"]["audio_stream_count"] == 0
    assert manifest["video"]["frame_count"] == 6
    assert manifest["timestamps"]["count"] == 6
    assert manifest["validation"]["timestamps_exact"]
    assert manifest["validation"]["full_decode_valid"]
    assert manifest["validation"]["jpeg_samples_checked"] == 6
    assert manifest["validation"]["jpeg_sample_indices"] == [0, 1, 2, 3, 4, 5]
    assert manifest["validation"]["jpeg_frames_validated_during_encoding"] == 6
    assert manifest["compression"]["encoding_time_to_video_time"] > 0
    assert manifest["source"]["seq_sha256"]
    assert manifest["video"]["video_sha256"]


@pytest.mark.integration
def test_encoding_progress_reaches_total_frames(
    tiny_seq: Path, tmp_path: Path, monkeypatch
) -> None:
    import seqcomp.encode as encode_module

    instances = []

    class RecordingProgress:
        def __init__(self, **kwargs):
            self.total = kwargs["total"]
            self.n = 0
            self.closed = False
            instances.append(self)

        def update(self, amount: int) -> None:
            self.n += amount

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(encode_module, "tqdm", RecordingProgress)
    runtime = inspect_ffmpeg(require_environment=True)
    reader = SeqReader(tiny_seq)
    outputs = output_paths(tiny_seq, tmp_path)
    encode_segment(
        reader,
        runtime,
        DEFAULT_SETTINGS,
        outputs,
        show_progress=True,
    )
    assert len(instances) == 1
    assert instances[0].total == reader.frame_count
    assert instances[0].n == reader.frame_count
    assert instances[0].closed


@pytest.mark.integration
def test_compression_reports_encoding_and_validation_progress(
    tiny_seq: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    import seqcomp.compress as compress_module
    import seqcomp.encode as encode_module

    instances = []

    class RecordingProgress:
        def __init__(self, **kwargs):
            self.total = kwargs["total"]
            self.description = kwargs["desc"]
            self.n = 0
            self.closed = False
            instances.append(self)

        def update(self, amount: int) -> None:
            self.n += amount

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(encode_module, "tqdm", RecordingProgress)
    monkeypatch.setattr(compress_module, "tqdm", RecordingProgress)
    runtime = inspect_ffmpeg(require_environment=True)
    reader = SeqReader(tiny_seq)
    compress_recording(
        tiny_seq,
        tmp_path,
        runtime,
        DEFAULT_SETTINGS,
        show_progress=True,
    )
    descriptions = {item.description for item in instances}
    assert f"Encoding {tiny_seq.name}" in descriptions
    assert f"Validate decode {tiny_seq.stem}.mkv" in descriptions
    assert all(item.total == reader.frame_count for item in instances)
    assert all(item.n == reader.frame_count for item in instances)
    assert all(item.closed for item in instances)
    output = capsys.readouterr().out
    assert "[read]" in output
    assert "[hash]" in output
    assert "[encode]" in output
    assert "[verify]" in output


@pytest.mark.integration
def test_compression_rejects_jpeg_header_dimension_mismatch(
    tiny_seq: Path, tmp_path: Path
) -> None:
    data = bytearray(tiny_seq.read_bytes())
    data[548:552] = (32).to_bytes(4, "little")
    tiny_seq.write_bytes(data)
    runtime = inspect_ffmpeg(require_environment=True)
    with pytest.raises(SeqFormatError, match="JPEG dimensions 31x29.*header 32x29"):
        compress_recording(tiny_seq, tmp_path / "output", runtime, DEFAULT_SETTINGS)


@pytest.mark.integration
def test_wrong_shaped_manifest_is_reported_as_unverified(
    tiny_seq: Path, tmp_path: Path
) -> None:
    runtime = inspect_ffmpeg(require_environment=True)
    outputs = compress_recording(tiny_seq, tmp_path, runtime, DEFAULT_SETTINGS)
    outputs.manifest.write_text("[]\n", encoding="utf-8")
    verified, reason, _ = verify_existing_package(
        tiny_seq, tmp_path, runtime, DEFAULT_SETTINGS
    )
    assert not verified
    assert reason == "manifest root must be a JSON object"
