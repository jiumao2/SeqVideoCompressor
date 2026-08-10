from io import BytesIO

import pytest

from seqcomp.encode import _follow_ffmpeg_progress
from seqcomp.encoding import DEFAULT_SETTINGS, make_settings


def test_default_settings_are_x265_crf18_medium() -> None:
    assert DEFAULT_SETTINGS.codec == "libx265"
    assert DEFAULT_SETTINGS.crf == 18
    assert DEFAULT_SETTINGS.preset == "medium"
    assert DEFAULT_SETTINGS.keyint == 250
    assert "gray" in DEFAULT_SETTINGS.ffmpeg_args()


def test_crf_zero_is_not_supported() -> None:
    with pytest.raises(ValueError, match="CRF 0"):
        make_settings(crf=0)


def test_ffmpeg_progress_parser_tracks_frames_and_ignores_bad_values() -> None:
    class Progress:
        n = 0

        def update(self, amount: int) -> None:
            self.n += amount

    progress = Progress()
    stream = BytesIO(
        b"frame=1\nout_time_us=1000\nframe=bad\nframe=4\nframe=99\nprogress=end\n"
    )
    _follow_ffmpeg_progress(stream, progress, frame_count=6)
    assert progress.n == 6
