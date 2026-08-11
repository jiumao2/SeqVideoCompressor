from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import create_test_seq
from seqcomp.compress import compress_recording, verify_existing_package
from seqcomp.core import compress_path
from seqcomp.encoding import EncodingSettings, make_settings
from seqcomp.ffmpeg_tools import GPUCapabilityError, inspect_nvenc


def _gpu_runtime_or_skip(settings: EncodingSettings):
    try:
        return inspect_nvenc(settings, require_environment=True)
    except GPUCapabilityError as exc:
        pytest.skip(str(exc).splitlines()[0])


@pytest.mark.integration
@pytest.mark.gpu
def test_nvenc_default_encodes_even_dimensions(tmp_path: Path) -> None:
    width, height = 1000, 900
    seq_path = create_test_seq(tmp_path, width=width, height=height)
    settings = make_settings(gpu=True)
    runtime = _gpu_runtime_or_skip(settings)
    outputs = compress_recording(seq_path, tmp_path / "output", runtime, settings)
    manifest = json.loads(outputs.manifest.read_text(encoding="utf-8"))
    probe = manifest["video"]["probe"]
    assert manifest["schema_version"] == 3
    assert manifest["encoding"]["mode"] == "gpu"
    assert manifest["encoding"]["codec"] == "hevc_nvenc"
    assert manifest["encoding"]["cq"] == 28
    assert manifest["encoding"]["preset"] == "p5"
    assert probe["codec_name"] == "hevc"
    assert probe["profile"] == "Main"
    assert probe["pix_fmt"] in {"yuv420p", "yuvj420p"}
    assert probe["color_range"] == "pc"
    assert probe["color_space"] == "bt470bg"
    assert (probe["width"], probe["height"]) == (width, height)
    assert probe["audio_stream_count"] == 0
    assert manifest["validation"]["full_decode_valid"] is True
    assert manifest["timestamps"]["count"] == 6


@pytest.mark.integration
@pytest.mark.gpu
def test_nvenc_rejects_odd_dimensions_without_padding(tmp_path: Path) -> None:
    seq_path = create_test_seq(tmp_path, width=999, height=899)
    settings = make_settings(gpu=True)
    runtime = _gpu_runtime_or_skip(settings)
    with pytest.raises(ValueError, match="requires even frame dimensions"):
        compress_recording(seq_path, tmp_path / "output", runtime, settings)
    assert not (tmp_path / "output" / "test-recording.000.mkv").exists()


@pytest.mark.integration
@pytest.mark.gpu
def test_existing_nvenc_package_is_verified_then_deleted_without_reencoding(
    tmp_path: Path,
) -> None:
    seq_path = create_test_seq(tmp_path, width=1000, height=900)
    idx_path = Path(f"{seq_path}.idx")
    settings = make_settings(gpu=True)
    runtime = _gpu_runtime_or_skip(settings)
    first = compress_path(seq_path, runtime, settings, quiet=True)
    assert (first.compressed, first.deleted, first.failed) == (1, 0, 0)
    second = compress_path(seq_path, runtime, settings, delete=True, quiet=True)
    assert (second.compressed, second.skipped, second.deleted, second.failed) == (
        0,
        1,
        1,
        0,
    )
    assert not seq_path.exists()
    assert not idx_path.exists()
    assert (tmp_path / "test-recording.000.mkv").is_file()


@pytest.mark.integration
@pytest.mark.gpu
def test_existing_gpu_package_is_not_reused_for_different_cq(tmp_path: Path) -> None:
    seq_path = create_test_seq(tmp_path, width=1000, height=900)
    original = make_settings(gpu=True, cq=28)
    runtime = _gpu_runtime_or_skip(original)
    compress_recording(seq_path, tmp_path / "output", runtime, original)
    verified, reason, _ = verify_existing_package(
        seq_path,
        tmp_path / "output",
        runtime,
        make_settings(gpu=True, cq=26),
    )
    assert not verified
    assert reason == "parameter mismatch: cq"
