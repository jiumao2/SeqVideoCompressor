from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import seqcomp.ffmpeg_tools as ffmpeg_tools
from seqcomp.encoding import make_settings
from seqcomp.ffmpeg_tools import (
    FFmpegCapabilityError,
    FFmpegRuntime,
    GPUCapabilityError,
)


def test_nvenc_error_explains_missing_encoder(monkeypatch) -> None:
    settings = make_settings(gpu=True)
    ffmpeg_tools.inspect_nvenc.cache_clear()
    runtime = FFmpegRuntime(
        Path("ffmpeg.exe"),
        Path("ffprobe.exe"),
        "ffmpeg version test",
        (),
    )

    def inspect(**kwargs):
        if kwargs["required_encoders"]:
            raise FFmpegCapabilityError("FFmpeg lacks required encoders: hevc_nvenc")
        return runtime

    monkeypatch.setattr(ffmpeg_tools, "inspect_ffmpeg", inspect)
    monkeypatch.setattr(
        ffmpeg_tools, "_nvidia_diagnostics", lambda: "0, Test GPU, 600.00"
    )
    with pytest.raises(GPUCapabilityError) as exc_info:
        ffmpeg_tools.inspect_nvenc(settings)
    message = str(exc_info.value)
    assert "NVIDIA H.265 NVENC is unavailable" in message
    assert "hevc_nvenc encoder" in message
    assert "ffmpeg.exe (ffmpeg version test)" in message
    assert "Omit --gpu to use CPU encoding" in message
    ffmpeg_tools.inspect_nvenc.cache_clear()


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("Cannot load nvcuda.dll", "CUDA driver library is unavailable"),
        ("No capable devices found", "No NVENC-capable NVIDIA GPU"),
        ("OpenEncodeSessionEx failed: out of memory", "GPU resources may be exhausted"),
    ],
)
def test_nvenc_failure_categories(stderr: str, expected: str) -> None:
    assert expected in ffmpeg_tools._gpu_failure_reason(stderr)


def test_nvenc_error_identifies_invalid_requested_device(monkeypatch) -> None:
    settings = make_settings(gpu=True, gpu_device=4)
    runtime = FFmpegRuntime(
        Path("ffmpeg.exe"),
        Path("ffprobe.exe"),
        "ffmpeg version test",
        ("hevc_nvenc",),
    )
    monkeypatch.setattr(
        ffmpeg_tools,
        "_nvidia_diagnostics",
        lambda: "0, Test GPU, 600.00\n1, Second GPU, 600.00",
    )
    error = ffmpeg_tools._gpu_error(runtime, settings, "No capable devices found")
    assert "GPU device index 4 is invalid" in str(error)
    assert "available index(es): 0, 1" in str(error)


def test_nvenc_error_preserves_old_driver_diagnostics(monkeypatch) -> None:
    settings = make_settings(gpu=True, gpu_device=2)
    runtime = FFmpegRuntime(
        Path("ffmpeg.exe"),
        Path("ffprobe.exe"),
        "ffmpeg version test",
        ("hevc_nvenc",),
    )
    ffmpeg_tools.inspect_nvenc.cache_clear()
    monkeypatch.setattr(ffmpeg_tools, "inspect_ffmpeg", lambda **kwargs: runtime)
    monkeypatch.setattr(
        ffmpeg_tools,
        "_nvidia_diagnostics",
        lambda: "2, Test GPU, 500.00",
    )
    monkeypatch.setattr(
        ffmpeg_tools.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="Driver does not support the required nvenc API version",
        ),
    )
    with pytest.raises(GPUCapabilityError) as exc_info:
        ffmpeg_tools.inspect_nvenc(settings)
    message = str(exc_info.value)
    assert "driver is too old" in message
    assert "device=2" in message
    assert "Driver does not support" in message
    assert "Test GPU" in message
    ffmpeg_tools.inspect_nvenc.cache_clear()
