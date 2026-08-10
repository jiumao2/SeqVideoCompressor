from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class OutputPaths:
    video: Path
    timestamps: Path
    manifest: Path


def source_basename(seq_path: str | Path) -> str:
    """Return the input filename without only its final .seq suffix."""
    path = Path(seq_path)
    if path.suffix.lower() != ".seq":
        raise ValueError(f"Expected a .seq input, got: {path}")
    return path.name[: -len(path.suffix)]


def output_paths(
    seq_path: str | Path,
    output_dir: str | Path,
) -> OutputPaths:
    parts = [source_basename(seq_path)]
    stem = ".".join(parts)
    root = Path(output_dir)
    return OutputPaths(
        video=root / f"{stem}.mkv",
        timestamps=root / f"{stem}.timestamps.npy",
        manifest=root / f"{stem}.manifest.json",
    )


def sidecars_for_video(video_path: str | Path) -> tuple[Path, Path]:
    video = Path(video_path)
    return (
        video.with_name(f"{video.stem}.timestamps.npy"),
        video.with_name(f"{video.stem}.manifest.json"),
    )
