from io import BytesIO

import pytest

from seqcomp.encode import _follow_ffmpeg_progress
from seqcomp.encoding import DEFAULT_GPU_SETTINGS, DEFAULT_SETTINGS, make_settings


def test_default_settings_are_x265_crf18_medium() -> None:
    assert DEFAULT_SETTINGS.codec == "libx265"
    assert DEFAULT_SETTINGS.crf == 18
    assert DEFAULT_SETTINGS.preset == "medium"
    assert DEFAULT_SETTINGS.keyint == 250
    assert DEFAULT_SETTINGS.ffmpeg_args() == [
        "-c:v",
        "libx265",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-pix_fmt",
        "gray",
        "-x265-params",
        "keyint=250:min-keyint=250:scenecut=0:open-gop=0:log-level=error",
    ]


def test_default_gpu_settings_are_nvenc_cq28_p5() -> None:
    settings = DEFAULT_GPU_SETTINGS
    assert settings.codec == "hevc_nvenc"
    assert settings.codec_family == "h265"
    assert settings.crf is None
    assert settings.cq == 28
    assert settings.preset == "p5"
    assert settings.keyint == 250
    assert settings.ffmpeg_args() == [
        "-vf",
        "scale=in_range=full:out_range=full,format=yuv420p",
        "-color_range",
        "pc",
        "-colorspace",
        "bt470bg",
        "-c:v",
        "hevc_nvenc",
        "-preset",
        "p5",
        "-tune",
        "hq",
        "-rc",
        "vbr",
        "-cq",
        "28",
        "-b:v",
        "0",
        "-multipass",
        "fullres",
        "-rc-lookahead",
        "32",
        "-spatial-aq",
        "1",
        "-temporal-aq",
        "1",
        "-aq-strength",
        "8",
        "-g",
        "250",
        "-no-scenecut",
        "1",
        "-bf",
        "3",
        "-b_ref_mode",
        "middle",
        "-profile:v",
        "main",
        "-pix_fmt",
        "yuv420p",
    ]
    manifest = settings.manifest_encoding()
    assert manifest["mode"] == "gpu"
    assert manifest["codec_family"] == "h265"
    assert manifest["quality_mode"] == "cq"
    assert manifest["multipass"] == "fullres"
    assert manifest["rc_lookahead"] == 32
    assert manifest["spatial_aq"] is True
    assert manifest["temporal_aq"] is True
    assert manifest["scene_cut"] is False
    assert manifest["b_frames"] == 3
    assert manifest["b_ref_mode"] == "middle"


def test_gpu_settings_allow_cq_preset_device_and_keyint() -> None:
    settings = make_settings(
        gpu=True, cq=26, gpu_preset="p6", gpu_device=1, keyint=500
    )
    assert (settings.cq, settings.preset, settings.gpu_device, settings.keyint) == (
        26,
        "p6",
        1,
        500,
    )
    arguments = settings.ffmpeg_args()
    assert arguments[arguments.index("-gpu") + 1] == "1"
    assert arguments[arguments.index("-g") + 1] == "500"


def test_av1_defaults_are_svtav1_crf28_preset6() -> None:
    settings = make_settings(codec="av1")
    assert settings.codec == "libsvtav1"
    assert settings.codec_family == "av1"
    assert settings.ffprobe_codec == "av1"
    assert settings.crf == 28
    assert settings.preset == 6
    assert settings.keyint == 250
    assert settings.name == "svtav1-crf28-preset6"
    assert settings.ffmpeg_args() == [
        "-vf",
        "scale=in_range=full:out_range=full,format=yuv420p",
        "-color_range",
        "pc",
        "-colorspace",
        "bt470bg",
        "-c:v",
        "libsvtav1",
        "-preset",
        "6",
        "-crf",
        "28",
        "-g",
        "250",
        "-pix_fmt",
        "yuv420p",
    ]
    assert settings.manifest_encoding() == {
        "mode": "cpu",
        "codec": "libsvtav1",
        "codec_family": "av1",
        "quality_mode": "crf",
        "preset": 6,
        "keyint": 250,
        "container": "matroska",
        "audio": "none",
        "crf": 28,
        "profile": "main",
        "pixel_format": "yuv420p",
        "color_range": "pc",
        "color_space": "bt470bg",
    }


def test_av1_allows_independent_quality_effort_and_keyint() -> None:
    settings = make_settings(codec="av1", crf=24, av1_preset=8, keyint=500)
    assert (settings.crf, settings.preset, settings.keyint) == (24, 8, 500)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"codec": "av1", "crf": 64}, "AV1 crf"),
        ({"codec": "av1", "av1_preset": -1}, "AV1 preset"),
        ({"codec": "av1", "av1_preset": 14}, "AV1 preset"),
        ({"codec": "av1", "preset": "medium"}, "use --av1-preset"),
        ({"codec": "h265", "av1_preset": 6}, "requires --codec av1"),
        ({"gpu": True, "av1_preset": 6}, "cannot be used with --gpu"),
        ({"codec": "av1", "gpu": True}, "only --codec h265"),
    ],
)
def test_av1_rejects_invalid_or_conflicting_parameters(
    kwargs: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        make_settings(**kwargs)


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
