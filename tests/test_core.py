from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import create_test_seq
from seqcomp.cli import _parser
from seqcomp.core import (
    FOLDER_MANIFEST_NAME,
    TEMPORARY_MINIMUM_AGE_SECONDS,
    compress_path,
    format_duration,
    format_gb,
    iter_seq_pairs,
    status_path,
    status_report,
)
from seqcomp.ffmpeg_tools import inspect_ffmpeg, probe_video
from seqcomp.encoding import make_settings
from seqcomp.runtime_utils import sha256_file


def _runtime_and_settings():
    return inspect_ffmpeg(require_environment=True), make_settings()


def _old_temporary_video(
    folder: Path,
    stem: str = "test-recording.000",
    token: str = "abcdefgh",
) -> Path:
    temporary = folder / f".{stem}.{token}.seqcomp.tmp.mkv"
    temporary.write_bytes(b"interrupted partial encode")
    old = time.time() - TEMPORARY_MINIMUM_AGE_SECONDS - 60
    os.utime(temporary, (old, old))
    return temporary


def test_pair_discovery_is_recursive_and_requires_seq_idx(tmp_path: Path) -> None:
    source = tmp_path / "source"
    first = create_test_seq(source, name="first.000.seq")
    second = create_test_seq(source / "nested", name="second.000.seq")
    orphan = source / "orphan.seq"
    orphan.write_bytes(b"not a recording")
    assert [pair.seq for pair in iter_seq_pairs(source)] == [first, second]


def test_cli_size_and_duration_formatting() -> None:
    assert format_gb(12_830_000_000) == "12.83 GB"
    assert format_gb(520_000_000) == "0.52 GB"
    assert format_duration(25) == "25.0 s"
    assert format_duration(125) == "2 m 5 s"
    assert format_duration(3725) == "1 h 2 m 5 s"


def test_status_report_summarizes_all_raw_recordings(tmp_path: Path) -> None:
    source = tmp_path / "source"
    create_test_seq(source, name="first.000.seq")
    create_test_seq(source / "nested", name="second.000.seq")
    report = status_report(source, compression_ratio=10, encoding_time_ratio=2)
    assert report.startswith(f"Status: {source.resolve()}")
    assert "Estimates: 10x compression, 2x video duration for encoding" in report
    assert "[raw-only] first.000.seq" in report
    assert "[raw-only] nested/second.000.seq" in report
    assert "Recordings: 2 | raw-only 2" in report
    assert "Total space saving:" in report
    assert "Remaining raw-only:" in report
    assert "estimated encoding:" in report


def test_status_excludes_seqcomp_temporary_mkv_from_recording_counts(
    tmp_path: Path,
) -> None:
    temporary = _old_temporary_video(
        tmp_path,
        "20260712-15-42-58.000",
        "_9lzcxzw",
    )
    assert status_path(tmp_path) == []
    report = status_report(tmp_path)
    assert "Recordings: 0" in report
    assert "incomplete 0" in report
    assert "Temporary files" in report
    assert "Detected: 1" in report
    assert format_gb(temporary.stat().st_size) in report
    assert "--cleanup-temp" in report


def test_similar_user_mkv_is_not_treated_as_seqcomp_temporary(
    tmp_path: Path,
) -> None:
    user_file = tmp_path / ".user-video.not-a-python-token.seqcomp.tmp.mkv"
    user_file.write_bytes(b"user data")
    rows = status_path(tmp_path)
    assert len(rows) == 1
    assert rows[0]["state"] == "incomplete"


def test_cleanup_temp_keeps_file_without_verified_package(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    temporary = _old_temporary_video(source)

    result = compress_path(
        source,
        SimpleNamespace(),
        make_settings(),
        cleanup_temp=True,
        quiet=True,
    )
    assert result.ok
    assert result.temporary_cleaned == 0
    assert result.temporary_kept == 1
    assert temporary.is_file()


def test_cleanup_temp_keeps_file_when_removal_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from seqcomp import core

    source = tmp_path / "source"
    source.mkdir()
    temporary = _old_temporary_video(source)
    monkeypatch.setattr(
        core,
        "verify_standalone_package",
        lambda *args, **kwargs: (True, "verified", {"source": {}}),
    )
    original_unlink = Path.unlink

    def fail_temporary_unlink(path: Path, *args, **kwargs) -> None:
        if path == temporary:
            raise PermissionError("file is in use")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_temporary_unlink)
    result = compress_path(
        source,
        SimpleNamespace(),
        make_settings(),
        cleanup_temp=True,
        quiet=True,
    )
    assert result.ok
    assert result.temporary_cleaned == 0
    assert result.temporary_kept == 1
    assert result.temporary_bytes_cleaned == 0
    assert temporary.is_file()


@pytest.mark.integration
def test_folder_backup_compresses_pairs_and_mirrors_everything_else(tmp_path: Path) -> None:
    runtime, settings = _runtime_and_settings()
    source = tmp_path / "source"
    destination = tmp_path / "backup"
    first = create_test_seq(source, name="first.000.seq")
    second = create_test_seq(source / "nested", name="second.000.seq")
    note = source / "nested" / "notes.txt"
    note.write_text("metadata\n", encoding="utf-8")
    (source / "empty").mkdir()
    orphan = source / "orphan.seq"
    orphan.write_bytes(b"leave unchanged")

    summary = compress_path(source, runtime, settings, dest=destination, yes=True)

    assert summary.ok
    assert summary.compressed == 2
    assert summary.copied == 2
    assert not (destination / first.name).exists()
    assert not (destination / f"{first.name}.idx").exists()
    assert (destination / "first.000.mkv").is_file()
    assert (destination / "first.000.timestamps.npy").is_file()
    assert (destination / "nested" / "second.000.mkv").is_file()
    assert (destination / "nested" / "notes.txt").read_bytes() == note.read_bytes()
    assert (destination / "orphan.seq").read_bytes() == orphan.read_bytes()
    assert (destination / "empty").is_dir()
    assert probe_video(runtime, destination / "first.000.mkv")["audio_stream_count"] == 0
    assert {row["state"] for row in status_path(destination)} == {"compressed-only"}
    report = status_report(destination)
    assert "compressed-only 2" in report
    assert "source size unknown" not in report
    assert "Total space saving:" in report

    folder_manifest = json.loads(
        (destination / FOLDER_MANIFEST_NAME).read_text(encoding="utf-8")
    )
    assert set(folder_manifest["recordings"]) == {
        "first.000.seq",
        "nested/second.000.seq",
    }
    copied = folder_manifest["other_files"]["nested/notes.txt"]
    assert copied["source_sha256"] == sha256_file(note)
    assert copied["destination_sha256"] == sha256_file(destination / "nested" / "notes.txt")


@pytest.mark.integration
def test_rerun_skips_verified_content_and_repairs_tampering(tmp_path: Path) -> None:
    runtime, settings = _runtime_and_settings()
    source = tmp_path / "source"
    destination = tmp_path / "backup"
    create_test_seq(source)
    (source / "notes.txt").write_text("same", encoding="utf-8")
    first = compress_path(source, runtime, settings, dest=destination, yes=True)
    assert first.compressed == 1

    second = compress_path(source, runtime, settings, dest=destination, yes=True)
    assert second.compressed == 0
    assert second.skipped >= 2

    video = destination / "test-recording.000.mkv"
    with video.open("ab") as stream:
        stream.write(b"tamper")
    repaired = compress_path(source, runtime, settings, dest=destination, yes=True)
    assert repaired.ok
    assert repaired.compressed == 1


@pytest.mark.integration
def test_cleanup_temp_verifies_completed_package_without_source_pair(
    tmp_path: Path,
) -> None:
    runtime, settings = _runtime_and_settings()
    source = tmp_path / "source"
    seq = create_test_seq(source)
    idx = Path(f"{seq}.idx")
    first = compress_path(source, runtime, settings, yes=True, quiet=True)
    assert first.compressed == 1
    temporary = _old_temporary_video(source)
    size = temporary.stat().st_size
    seq.unlink()
    idx.unlink()

    cleaned = compress_path(
        source,
        runtime,
        settings,
        cleanup_temp=True,
        quiet=True,
    )
    assert cleaned.ok
    assert cleaned.compressed == 0
    assert cleaned.temporary_cleaned == 1
    assert cleaned.temporary_kept == 0
    assert cleaned.temporary_bytes_cleaned == size
    assert not temporary.exists()
    assert (source / "test-recording.000.mkv").is_file()


@pytest.mark.integration
def test_cleanup_temp_dry_run_preserves_eligible_file(tmp_path: Path) -> None:
    runtime, settings = _runtime_and_settings()
    source = tmp_path / "source"
    seq = create_test_seq(source)
    idx = Path(f"{seq}.idx")
    compress_path(source, runtime, settings, yes=True, quiet=True)
    temporary = _old_temporary_video(source)
    seq.unlink()
    idx.unlink()

    preview = compress_path(
        source,
        runtime,
        settings,
        cleanup_temp=True,
        dry_run=True,
        quiet=True,
    )
    assert preview.temporary_cleaned == 1
    assert preview.temporary_kept == 0
    assert temporary.is_file()


@pytest.mark.integration
def test_cleanup_temp_keeps_recent_or_unverified_files(tmp_path: Path) -> None:
    runtime, settings = _runtime_and_settings()
    source = tmp_path / "source"
    seq = create_test_seq(source)
    idx = Path(f"{seq}.idx")
    compress_path(source, runtime, settings, yes=True, quiet=True)
    recent = source / ".test-recording.000.abcdefgh.seqcomp.tmp.mkv"
    recent.write_bytes(b"active-looking encode")
    seq.unlink()
    idx.unlink()

    protected = compress_path(
        source,
        runtime,
        settings,
        cleanup_temp=True,
        quiet=True,
    )
    assert protected.temporary_cleaned == 0
    assert protected.temporary_kept == 1
    assert recent.is_file()

    old = time.time() - TEMPORARY_MINIMUM_AGE_SECONDS - 60
    os.utime(recent, (old, old))
    manifest_path = source / "test-recording.000.manifest.json"
    original_manifest = manifest_path.read_text(encoding="utf-8")
    wrong_identity = json.loads(original_manifest)
    wrong_identity["source"]["seq_path"] = "other-recording.000.seq"
    manifest_path.write_text(json.dumps(wrong_identity), encoding="utf-8")
    wrong_package = compress_path(
        source,
        runtime,
        settings,
        cleanup_temp=True,
        quiet=True,
    )
    assert wrong_package.temporary_cleaned == 0
    assert wrong_package.temporary_kept == 1
    assert recent.is_file()

    manifest_path.write_text(original_manifest, encoding="utf-8")
    with (source / "test-recording.000.mkv").open("ab") as stream:
        stream.write(b"tampered")
    unverified = compress_path(
        source,
        runtime,
        settings,
        cleanup_temp=True,
        quiet=True,
    )
    assert unverified.temporary_cleaned == 0
    assert unverified.temporary_kept == 1
    assert recent.is_file()


@pytest.mark.integration
def test_delete_occurs_only_after_verified_backup(tmp_path: Path, monkeypatch) -> None:
    runtime, settings = _runtime_and_settings()
    source = tmp_path / "source"
    destination = tmp_path / "backup"
    seq = create_test_seq(source)
    idx = Path(f"{seq}.idx")

    dry = compress_path(
        source, runtime, settings, dest=destination, delete=True, dry_run=True, yes=True
    )
    assert dry.compressed == 1
    assert seq.exists() and idx.exists() and not destination.exists()

    import seqcomp.core as core

    def fail(*args, **kwargs):
        raise RuntimeError("simulated encoder failure")

    monkeypatch.setattr(core, "compress_recording", fail)
    failed = compress_path(source, runtime, settings, dest=destination, delete=True, yes=True)
    assert failed.failed == 1
    assert failed.deleted == 0
    assert seq.exists() and idx.exists()


@pytest.mark.integration
def test_delete_removes_only_the_verified_source_pair(tmp_path: Path) -> None:
    runtime, settings = _runtime_and_settings()
    source = tmp_path / "source"
    destination = tmp_path / "backup"
    seq = create_test_seq(source)
    idx = Path(f"{seq}.idx")
    note = source / "notes.txt"
    note.write_text("keep", encoding="utf-8")

    summary = compress_path(source, runtime, settings, dest=destination, delete=True, yes=True)
    assert summary.ok and summary.deleted == 1
    assert not seq.exists() and not idx.exists()
    assert note.read_text(encoding="utf-8") == "keep"
    assert (destination / "test-recording.000.mkv").is_file()
    assert status_path(destination)[0]["state"] == "compressed-only"


@pytest.mark.integration
def test_delete_reuses_an_existing_verified_package(
    tmp_path: Path, monkeypatch
) -> None:
    runtime, settings = _runtime_and_settings()
    source = tmp_path / "source"
    destination = tmp_path / "backup"
    seq = create_test_seq(source)
    idx = Path(f"{seq}.idx")
    first = compress_path(source, runtime, settings, dest=destination, yes=True)
    assert first.ok and first.compressed == 1

    import seqcomp.core as core

    def unexpected_recompression(*args, **kwargs):
        raise AssertionError("verified output must not be recompressed")

    monkeypatch.setattr(core, "compress_recording", unexpected_recompression)
    second = compress_path(
        source, runtime, settings, dest=destination, delete=True, yes=True
    )
    assert second.ok
    assert second.compressed == 0
    assert second.skipped >= 1
    assert second.deleted == 1
    assert not seq.exists() and not idx.exists()
    assert (destination / "test-recording.000.mkv").is_file()


@pytest.mark.integration
def test_each_verified_pair_is_deleted_before_a_changed_later_pair(
    tmp_path: Path, monkeypatch
) -> None:
    runtime, settings = _runtime_and_settings()
    source = tmp_path / "source"
    destination = tmp_path / "backup"
    first = create_test_seq(source, name="first.000.seq")
    second = create_test_seq(source, name="second.000.seq")
    first_idx = Path(f"{first}.idx")
    second_idx = Path(f"{second}.idx")

    import seqcomp.core as core

    original_compress_recording = core.compress_recording

    def compress_then_modify(*args, **kwargs):
        outputs = original_compress_recording(*args, **kwargs)
        if Path(args[0]) == second:
            data = bytearray(second.read_bytes())
            data[100] ^= 1
            second.write_bytes(data)
        return outputs

    monkeypatch.setattr(core, "compress_recording", compress_then_modify)
    summary = compress_path(
        source, runtime, settings, dest=destination, delete=True, yes=True
    )
    assert summary.failed == 1
    assert summary.deleted == 1
    assert not first.exists() and not first_idx.exists()
    assert second.exists() and second_idx.exists()


@pytest.mark.integration
def test_later_encoding_failure_does_not_delay_an_earlier_verified_deletion(
    tmp_path: Path, monkeypatch
) -> None:
    runtime, settings = _runtime_and_settings()
    source = tmp_path / "source"
    destination = tmp_path / "backup"
    first = create_test_seq(source, name="first.000.seq")
    second = create_test_seq(source, name="second.000.seq")
    first_idx = Path(f"{first}.idx")
    second_idx = Path(f"{second}.idx")

    import seqcomp.core as core

    original_compress_recording = core.compress_recording

    def fail_on_second(*args, **kwargs):
        if Path(args[0]) == second:
            raise RuntimeError("simulated later encoder failure")
        return original_compress_recording(*args, **kwargs)

    monkeypatch.setattr(core, "compress_recording", fail_on_second)
    summary = compress_path(
        source, runtime, settings, dest=destination, delete=True, yes=True
    )
    assert summary.failed == 1
    assert summary.deleted == 1
    assert not first.exists() and not first_idx.exists()
    assert second.exists() and second_idx.exists()
    assert (destination / "first.000.mkv").is_file()


@pytest.mark.integration
def test_later_copy_failure_does_not_restore_a_verified_deletion(
    tmp_path: Path, monkeypatch
) -> None:
    runtime, settings = _runtime_and_settings()
    source = tmp_path / "source"
    destination = tmp_path / "backup"
    seq = create_test_seq(source)
    idx = Path(f"{seq}.idx")
    note = source / "notes.txt"
    note.write_text("metadata", encoding="utf-8")

    import seqcomp.core as core

    def fail_copy(*args, **kwargs):
        raise RuntimeError("simulated later copy failure")

    monkeypatch.setattr(core, "_copy_verified", fail_copy)
    summary = compress_path(
        source, runtime, settings, dest=destination, delete=True, yes=True
    )
    assert summary.failed == 1
    assert summary.deleted == 1
    assert not seq.exists() and not idx.exists()
    assert note.exists()
    assert (destination / "test-recording.000.mkv").is_file()


@pytest.mark.integration
def test_folder_run_repairs_wrong_shaped_manifest(tmp_path: Path) -> None:
    runtime, settings = _runtime_and_settings()
    source = tmp_path / "source"
    destination = tmp_path / "backup"
    create_test_seq(source)
    first = compress_path(source, runtime, settings, dest=destination, yes=True)
    assert first.ok and first.compressed == 1
    manifest = destination / "test-recording.000.manifest.json"
    manifest.write_text("[]\n", encoding="utf-8")

    repaired = compress_path(source, runtime, settings, dest=destination, yes=True)
    assert repaired.ok and repaired.compressed == 1
    assert isinstance(json.loads(manifest.read_text(encoding="utf-8")), dict)


def test_status_and_cli_use_independent_codec_arguments(tmp_path: Path) -> None:
    seq = create_test_seq(tmp_path)
    assert status_path(tmp_path)[0]["state"] == "raw-only"
    parser = _parser()
    args = parser.parse_args(
        ["compress", str(tmp_path), "--codec", "h264", "--crf", "20", "--preset", "fast"]
    )
    assert (args.codec, args.crf, args.preset) == ("h264", 20, "fast")
    help_text = parser.format_help()
    assert "--profile" not in help_text
    assert "--skip-hashes" not in help_text
    assert "benchmark" not in help_text
    assert "tune" not in help_text
    assert "verify" not in help_text
    assert seq.exists()


@pytest.mark.integration
def test_single_file_allows_a_destination_below_its_parent(tmp_path: Path) -> None:
    runtime, settings = _runtime_and_settings()
    seq = create_test_seq(tmp_path)
    destination = tmp_path / "compressed"
    summary = compress_path(seq, runtime, settings, dest=destination, yes=True)
    assert summary.ok and summary.compressed == 1
    assert (destination / "test-recording.000.mkv").is_file()


def test_space_preflight_refuses_before_writing_or_deleting(
    tmp_path: Path, monkeypatch
) -> None:
    runtime, settings = _runtime_and_settings()
    source = tmp_path / "source"
    seq = create_test_seq(source)
    idx = Path(f"{seq}.idx")
    import seqcomp.core as core

    monkeypatch.setattr(core.shutil, "disk_usage", lambda path: SimpleNamespace(free=0))
    with pytest.raises(OSError, match="Insufficient free space"):
        compress_path(source, runtime, settings, delete=True, yes=True)
    assert seq.exists() and idx.exists()
    assert not (source / "test-recording.000.mkv").exists()


def test_delete_same_volume_space_preflight_uses_one_recording_peak(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source"
    create_test_seq(source, name="first.000.seq")
    create_test_seq(source, name="second.000.seq")
    pairs = list(iter_seq_pairs(source))
    requirements = [
        (pair.seq.stat().st_size + pair.idx.stat().st_size) // 2
        + 16 * 1024 * 1024
        for pair in pairs
    ]

    import seqcomp.core as core

    monkeypatch.setattr(
        core.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(free=max(requirements)),
    )
    core._require_space(source, None, pairs, is_file=False, delete=True)

    with pytest.raises(OSError, match="Insufficient free space"):
        core._require_space(source, None, pairs, is_file=False, delete=False)


def test_delete_different_volume_space_preflight_remains_cumulative(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    create_test_seq(source, name="first.000.seq")
    create_test_seq(source, name="second.000.seq")
    pairs = list(iter_seq_pairs(source))
    requirements = [
        (pair.seq.stat().st_size + pair.idx.stat().st_size) // 2
        + 16 * 1024 * 1024
        for pair in pairs
    ]

    import seqcomp.core as core

    monkeypatch.setattr(core, "_same_volume", lambda first, second: False)
    monkeypatch.setattr(
        core.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(free=max(requirements)),
    )
    with pytest.raises(OSError, match="Insufficient free space"):
        core._require_space(
            source,
            destination,
            pairs,
            is_file=False,
            delete=True,
        )
