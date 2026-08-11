from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


CODEC_NAMES = {"h264": "libx264", "h265": "libx265"}
PRESETS = (
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
)
GPU_PRESETS = ("p1", "p2", "p3", "p4", "p5", "p6", "p7")
DEFAULT_CRF = 18
DEFAULT_CPU_PRESET = "medium"
DEFAULT_CQ = 28
DEFAULT_GPU_PRESET = "p5"
DEFAULT_KEYINT = 250


@dataclass(frozen=True)
class EncodingSettings:
    codec: str
    crf: int | None
    preset: str
    keyint: int
    cq: int | None = None
    gpu_device: int | None = None

    @property
    def is_gpu(self) -> bool:
        return self.codec == "hevc_nvenc"

    @property
    def codec_family(self) -> str:
        return "h264" if self.codec == "libx264" else "h265"

    @property
    def ffprobe_codec(self) -> str:
        return "h264" if self.codec_family == "h264" else "hevc"

    @property
    def name(self) -> str:
        if self.is_gpu:
            return f"nvenc-cq{self.cq}-{self.preset}"
        short_codec = "x264" if self.codec == "libx264" else "x265"
        return f"{short_codec}-crf{self.crf}-{self.preset}"

    def ffmpeg_args(self) -> list[str]:
        if self.codec == "libx264":
            return [
                "-c:v",
                "libx264",
                "-preset",
                self.preset,
                "-crf",
                str(self.crf),
                "-pix_fmt",
                "gray",
                "-x264-params",
                f"keyint={self.keyint}:min-keyint={self.keyint}:scenecut=0:open-gop=0",
            ]
        if self.codec == "libx265":
            parameters = ":".join(
                [
                    f"keyint={self.keyint}",
                    f"min-keyint={self.keyint}",
                    "scenecut=0",
                    "open-gop=0",
                    "log-level=error",
                ]
            )
            return [
                "-c:v",
                "libx265",
                "-preset",
                self.preset,
                "-crf",
                str(self.crf),
                "-pix_fmt",
                "gray",
                "-x265-params",
                parameters,
            ]
        if self.codec == "hevc_nvenc":
            arguments = [
                "-vf",
                "scale=in_range=full:out_range=full,format=yuv420p",
                "-color_range",
                "pc",
                "-colorspace",
                "bt470bg",
                "-c:v",
                "hevc_nvenc",
                "-preset",
                self.preset,
                "-tune",
                "hq",
                "-rc",
                "vbr",
                "-cq",
                str(self.cq),
                "-b:v",
                "0",
                "-multipass",
                "fullres",
                "-rc-lookahead",
                "32",
                "-spatial_aq",
                "1",
                "-temporal_aq",
                "1",
                "-aq-strength",
                "8",
                "-g",
                str(self.keyint),
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
            if self.gpu_device is not None:
                arguments.extend(["-gpu", str(self.gpu_device)])
            return arguments
        raise ValueError(f"Unknown codec: {self.codec}")

    def manifest_encoding(self) -> dict[str, object]:
        common: dict[str, object] = {
            "mode": "gpu" if self.is_gpu else "cpu",
            "codec": self.codec,
            "codec_family": self.codec_family,
            "quality_mode": "cq" if self.is_gpu else "crf",
            "preset": self.preset,
            "keyint": self.keyint,
            "container": "matroska",
            "audio": "none",
        }
        if not self.is_gpu:
            common["crf"] = self.crf
            return common
        common.update(
            {
                "cq": self.cq,
                "gpu_device": self.gpu_device if self.gpu_device is not None else "auto",
                "tune": "hq",
                "rate_control": "vbr",
                "target_bitrate": 0,
                "multipass": "fullres",
                "rc_lookahead": 32,
                "spatial_aq": True,
                "temporal_aq": True,
                "aq_strength": 8,
                "scene_cut": False,
                "b_frames": 3,
                "b_ref_mode": "middle",
                "profile": "main",
                "pixel_format": "yuv420p",
                "color_range": "pc",
                "color_space": "bt470bg",
            }
        )
        return common

    def manifest_matches(
        self, recorded: Mapping[str, object], schema_version: int
    ) -> tuple[bool, str | None]:
        if schema_version == 2 and not self.is_gpu:
            expected = {
                "codec": self.codec,
                "preset": self.preset,
                "crf": self.crf,
                "keyint": self.keyint,
            }
        elif schema_version == 3:
            expected = self.manifest_encoding()
        else:
            return False, "manifest schema"
        for key, value in expected.items():
            if recorded.get(key) != value:
                return False, key
        return True, None


def make_settings(
    codec: str = "h265",
    crf: int | None = None,
    preset: str | None = None,
    keyint: int = DEFAULT_KEYINT,
    *,
    gpu: bool = False,
    cq: int | None = None,
    gpu_preset: str | None = None,
    gpu_device: int | None = None,
) -> EncodingSettings:
    if codec not in CODEC_NAMES:
        raise ValueError(f"codec must be one of: {', '.join(CODEC_NAMES)}")
    if keyint <= 0:
        raise ValueError("keyint must be positive")
    if gpu:
        if codec != "h265":
            raise ValueError("--gpu currently supports only --codec h265")
        if crf is not None:
            raise ValueError("--crf is CPU-only; use --cq with --gpu")
        if preset is not None:
            raise ValueError("--preset is CPU-only; use --gpu-preset with --gpu")
        selected_cq = DEFAULT_CQ if cq is None else cq
        selected_preset = DEFAULT_GPU_PRESET if gpu_preset is None else gpu_preset
        if not 1 <= selected_cq <= 51:
            raise ValueError("cq must be between 1 and 51; CQ 0 (automatic) is not supported")
        if selected_preset not in GPU_PRESETS:
            raise ValueError(f"gpu preset must be one of: {', '.join(GPU_PRESETS)}")
        if gpu_device is not None and gpu_device < 0:
            raise ValueError("gpu device must be a zero-based non-negative index")
        return EncodingSettings(
            "hevc_nvenc",
            None,
            selected_preset,
            keyint,
            selected_cq,
            gpu_device,
        )
    if cq is not None or gpu_preset is not None or gpu_device is not None:
        raise ValueError("--cq, --gpu-preset, and --gpu-device require --gpu")
    selected_crf = DEFAULT_CRF if crf is None else crf
    selected_preset = DEFAULT_CPU_PRESET if preset is None else preset
    if not 1 <= selected_crf <= 51:
        raise ValueError("crf must be between 1 and 51; CRF 0 is not supported")
    if selected_preset not in PRESETS:
        raise ValueError(f"preset must be one of: {', '.join(PRESETS)}")
    return EncodingSettings(
        CODEC_NAMES[codec], selected_crf, selected_preset, keyint
    )


DEFAULT_SETTINGS = make_settings()
DEFAULT_GPU_SETTINGS = make_settings(gpu=True)
