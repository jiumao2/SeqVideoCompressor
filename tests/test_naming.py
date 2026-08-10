from pathlib import Path

import pytest

from seqcomp.naming import output_paths, sidecars_for_video, source_basename


def test_source_basename_preserves_dot_zero_suffix() -> None:
    source = Path("20260726-20-12-06.000.seq")
    assert source_basename(source) == "20260726-20-12-06.000"


def test_default_output_names(tmp_path: Path) -> None:
    source = Path("20260726-20-12-06.000.seq")
    default = output_paths(source, tmp_path)
    assert default.video.name == "20260726-20-12-06.000.mkv"
    assert default.timestamps.name == "20260726-20-12-06.000.timestamps.npy"
    assert default.manifest.name == "20260726-20-12-06.000.manifest.json"
    assert sidecars_for_video(default.video) == (
        default.timestamps,
        default.manifest,
    )


def test_source_basename_rejects_non_seq() -> None:
    with pytest.raises(ValueError):
        source_basename("recording.avi")
