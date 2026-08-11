from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import create_test_seq
from seqcomp.cli import main
from seqcomp.compress import compress_recording, verify_existing_package
from seqcomp.core import compress_path
from seqcomp.encoding import make_settings
from seqcomp.ffmpeg_tools import inspect_ffmpeg


def _av1_runtime():
    return inspect_ffmpeg(
        require_environment=True,
        required_encoders=("libsvtav1",),
    )


@pytest.mark.integration
def test_cli_compresses_with_explicit_cpu_av1_settings(
    tmp_path: Path, capsys
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "output"
    create_test_seq(source, width=1000, height=900)
    code = main(
        [
            "compress",
            str(source),
            "--dest",
            str(destination),
            "--codec",
            "av1",
            "--crf",
            "28",
            "--av1-preset",
            "6",
            "--keyint",
            "250",
            "--yes",
            "--quiet",
        ]
    )
    assert code == 0
    manifest = json.loads(
        (destination / "test-recording.000.manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["encoding"] == make_settings(codec="av1").manifest_encoding()
    assert manifest["video"]["probe"]["codec_name"] == "av1"
    assert "Done: compressed 1" in capsys.readouterr().out


@pytest.mark.integration
@pytest.mark.parametrize(("width", "height"), [(1000, 900), (999, 899)])
def test_svtav1_package_is_fully_validated_for_even_and_odd_dimensions(
    tmp_path: Path, width: int, height: int
) -> None:
    seq_path = create_test_seq(tmp_path, width=width, height=height)
    settings = make_settings(codec="av1")
    outputs = compress_recording(
        seq_path,
        tmp_path / "output",
        _av1_runtime(),
        settings,
    )
    manifest = json.loads(outputs.manifest.read_text(encoding="utf-8"))
    probe = manifest["video"]["probe"]
    assert manifest["schema_version"] == 3
    assert manifest["encoding"] == settings.manifest_encoding()
    assert probe["codec_name"] == "av1"
    assert probe["profile"] == "Main"
    assert probe["pix_fmt"] in {"yuv420p", "yuvj420p"}
    assert probe["color_range"] == "pc"
    assert probe["color_space"] == "bt470bg"
    assert (probe["width"], probe["height"]) == (width, height)
    assert probe["audio_stream_count"] == 0
    assert manifest["video"]["frame_count"] == 6
    assert manifest["timestamps"]["count"] == 6
    assert manifest["validation"]["timestamps_exact"] is True
    assert manifest["validation"]["full_decode_valid"] is True
    assert manifest["source"]["seq_sha256"]
    assert manifest["source"]["idx_sha256"]
    assert manifest["video"]["video_sha256"]


@pytest.mark.integration
def test_existing_av1_package_is_reused_then_deleted_without_reencoding(
    tmp_path: Path, monkeypatch
) -> None:
    seq_path = create_test_seq(tmp_path, width=1000, height=900)
    idx_path = Path(f"{seq_path}.idx")
    settings = make_settings(codec="av1")
    runtime = _av1_runtime()
    first = compress_path(seq_path, runtime, settings, quiet=True)
    assert (first.compressed, first.deleted, first.failed) == (1, 0, 0)

    import seqcomp.core as core

    def unexpected_recompression(*args, **kwargs):
        raise AssertionError("verified AV1 output must not be recompressed")

    monkeypatch.setattr(core, "compress_recording", unexpected_recompression)
    second = compress_path(
        seq_path,
        runtime,
        settings,
        delete=True,
        quiet=True,
    )
    assert (second.compressed, second.skipped, second.deleted, second.failed) == (
        0,
        1,
        1,
        0,
    )
    assert not seq_path.exists()
    assert not idx_path.exists()


@pytest.mark.integration
def test_av1_package_is_not_reused_for_different_parameters(tmp_path: Path) -> None:
    seq_path = create_test_seq(tmp_path, width=1000, height=900)
    runtime = _av1_runtime()
    original = make_settings(codec="av1", crf=28, av1_preset=6)
    compress_recording(seq_path, tmp_path / "output", runtime, original)

    verified, reason, _ = verify_existing_package(
        seq_path,
        tmp_path / "output",
        runtime,
        make_settings(codec="av1", crf=26, av1_preset=6),
    )
    assert not verified
    assert reason == "parameter mismatch: crf"
