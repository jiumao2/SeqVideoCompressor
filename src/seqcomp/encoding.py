from __future__ import annotations

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


@dataclass(frozen=True)
class EncodingSettings:
    codec: str
    crf: int
    preset: str
    keyint: int

    @property
    def name(self) -> str:
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
        raise ValueError(f"Unknown codec: {self.codec}")


def make_settings(
    codec: str = "h265",
    crf: int = 18,
    preset: str = "medium",
    keyint: int = 250,
) -> EncodingSettings:
    if codec not in CODEC_NAMES:
        raise ValueError(f"codec must be one of: {', '.join(CODEC_NAMES)}")
    if not 1 <= crf <= 51:
        raise ValueError("crf must be between 1 and 51; CRF 0 is not supported")
    if preset not in PRESETS:
        raise ValueError(f"preset must be one of: {', '.join(PRESETS)}")
    if keyint <= 0:
        raise ValueError("keyint must be positive")
    return EncodingSettings(CODEC_NAMES[codec], crf, preset, keyint)


DEFAULT_SETTINGS = make_settings()
