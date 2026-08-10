from __future__ import annotations

import io
import struct
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator

import numpy as np


HEADER_SIZE = 1024
IDX_RECORD_SIZE = 24
IDX_DTYPE = np.dtype(
    [
        ("offset", "<u8"),
        ("buffer_size", "<u4"),
        ("seconds", "<i4"),
        ("milliseconds", "<u2"),
        ("microseconds", "<u2"),
        ("reserved", "<u4"),
    ]
)


class SeqFormatError(ValueError):
    """Raised when a SEQ/IDX pair violates the supported NorPix layout."""


JPEG_SOF_MARKERS = {
    0xC0,
    0xC1,
    0xC2,
    0xC3,
    0xC5,
    0xC6,
    0xC7,
    0xC9,
    0xCA,
    0xCB,
    0xCD,
    0xCE,
    0xCF,
}


def jpeg_dimensions(jpeg: bytes) -> tuple[int, int]:
    """Return JPEG width and height by parsing its SOF marker without decoding."""
    if len(jpeg) < 4 or not jpeg.startswith(b"\xff\xd8"):
        raise SeqFormatError("JPEG is missing its SOI marker")
    position = 2
    while position < len(jpeg):
        if jpeg[position] != 0xFF:
            raise SeqFormatError("Invalid JPEG marker layout before SOS")
        while position < len(jpeg) and jpeg[position] == 0xFF:
            position += 1
        if position >= len(jpeg):
            break
        marker = jpeg[position]
        position += 1
        if marker == 0xDA:
            break
        if marker in {0x01, 0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if position + 2 > len(jpeg):
            raise SeqFormatError("Truncated JPEG segment length")
        segment_length = int.from_bytes(jpeg[position : position + 2], "big")
        if segment_length < 2 or position + segment_length > len(jpeg):
            raise SeqFormatError("Invalid JPEG segment length")
        if marker in JPEG_SOF_MARKERS:
            if segment_length < 7:
                raise SeqFormatError("Truncated JPEG SOF segment")
            height = int.from_bytes(jpeg[position + 3 : position + 5], "big")
            width = int.from_bytes(jpeg[position + 5 : position + 7], "big")
            if width <= 0 or height <= 0:
                raise SeqFormatError("JPEG SOF contains invalid dimensions")
            return width, height
        position += segment_length
    raise SeqFormatError("JPEG does not contain a supported SOF marker")


@dataclass(frozen=True)
class SeqHeader:
    width: int
    height: int
    bit_depth: int
    real_bit_depth: int
    image_size_bytes: int
    image_format_code: int
    allocated_frames_u16: int
    origin: int
    true_image_size: int
    frame_rate: float
    description_format: int
    compression: int
    header_version: int
    header_size: int

    def to_dict(self) -> dict[str, int | float]:
        return asdict(self)


@dataclass(frozen=True)
class IndexValidation:
    frame_count: int
    first_offset: int
    final_end: int
    seq_size: int
    timestamps_monotonic: bool
    reserved_nonzero: int

    def to_dict(self) -> dict[str, int | bool]:
        return asdict(self)


def _read_header(path: Path) -> SeqHeader:
    with path.open("rb") as stream:
        data = stream.read(HEADER_SIZE)
    if len(data) != HEADER_SIZE:
        raise SeqFormatError(f"SEQ header is truncated: {path}")
    image_info = struct.unpack_from("<6I", data, 548)
    return SeqHeader(
        width=int(image_info[0]),
        height=int(image_info[1]),
        bit_depth=int(image_info[2]),
        real_bit_depth=int(image_info[3]),
        image_size_bytes=int(image_info[4]),
        image_format_code=int(image_info[5]),
        allocated_frames_u16=struct.unpack_from("<H", data, 572)[0],
        origin=struct.unpack_from("<I", data, 576)[0],
        true_image_size=struct.unpack_from("<I", data, 580)[0],
        frame_rate=struct.unpack_from("<d", data, 584)[0],
        description_format=struct.unpack_from("<I", data, 592)[0],
        compression=data[620],
        header_version=struct.unpack_from("<i", data, 28)[0],
        header_size=struct.unpack_from("<i", data, 32)[0],
    )


class SeqReader:
    """Read and validate JPEG-compressed NorPix SEQ files using the IDX."""

    def __init__(self, seq_path: str | Path, idx_path: str | Path | None = None):
        self.seq_path = Path(seq_path)
        self.idx_path = Path(idx_path) if idx_path else Path(f"{self.seq_path}.idx")
        if not self.seq_path.is_file():
            raise FileNotFoundError(self.seq_path)
        if not self.idx_path.is_file():
            raise FileNotFoundError(self.idx_path)
        self.header = _read_header(self.seq_path)
        idx_size = self.idx_path.stat().st_size
        if idx_size == 0 or idx_size % IDX_RECORD_SIZE:
            raise SeqFormatError(
                f"IDX size {idx_size} is not a positive multiple of {IDX_RECORD_SIZE}"
            )
        self.records = np.fromfile(self.idx_path, dtype=IDX_DTYPE)
        self.validation = self.validate_index()

    @property
    def frame_count(self) -> int:
        return int(self.records.size)

    @property
    def timestamps_us(self) -> np.ndarray:
        seconds = self.records["seconds"].astype(np.int64)
        milliseconds = self.records["milliseconds"].astype(np.int64)
        microseconds = self.records["microseconds"].astype(np.int64)
        return seconds * 1_000_000 + milliseconds * 1_000 + microseconds

    def validate_index(self) -> IndexValidation:
        if self.header.header_size != HEADER_SIZE:
            raise SeqFormatError(
                f"Unsupported header size {self.header.header_size}; expected {HEADER_SIZE}"
            )
        if self.header.compression != 1:
            raise SeqFormatError(
                f"Only JPEG-compressed SEQ is supported; compression={self.header.compression}"
            )
        offsets = self.records["offset"].astype(np.uint64)
        sizes = self.records["buffer_size"].astype(np.uint64)
        if int(offsets[0]) != HEADER_SIZE:
            raise SeqFormatError(
                f"First IDX offset is {int(offsets[0])}, expected {HEADER_SIZE}"
            )
        if np.any(sizes < 6):
            raise SeqFormatError("IDX contains an impossibly small JPEG buffer")
        expected = offsets[:-1] + sizes[:-1] + 8
        bad = np.flatnonzero(offsets[1:] != expected)
        if bad.size:
            frame = int(bad[0])
            raise SeqFormatError(f"IDX offsets are not contiguous after frame {frame}")
        final_end = int(offsets[-1] + sizes[-1] + 8)
        seq_size = self.seq_path.stat().st_size
        if final_end != seq_size:
            raise SeqFormatError(
                f"IDX final end {final_end} does not match SEQ size {seq_size}"
            )
        timestamps = self.timestamps_us
        monotonic = bool(np.all(np.diff(timestamps) > 0))
        if not monotonic:
            raise SeqFormatError("Acquisition timestamps are not strictly increasing")
        reserved_nonzero = int(np.count_nonzero(self.records["reserved"]))
        return IndexValidation(
            frame_count=self.frame_count,
            first_offset=int(offsets[0]),
            final_end=final_end,
            seq_size=seq_size,
            timestamps_monotonic=monotonic,
            reserved_nonzero=reserved_nonzero,
        )

    def _normalized_range(self, start: int, count: int | None) -> tuple[int, int]:
        if start < 0 or start >= self.frame_count:
            raise IndexError(f"start={start} outside [0, {self.frame_count})")
        stop = self.frame_count if count is None else start + count
        if count is not None and count <= 0:
            raise ValueError("count must be positive")
        if stop > self.frame_count:
            raise IndexError(f"stop={stop} exceeds frame count {self.frame_count}")
        return start, stop

    def read_jpeg_payload(
        self, frame_index: int, *, validate: bool = True
    ) -> bytes:
        if frame_index < 0 or frame_index >= self.frame_count:
            raise IndexError(frame_index)
        with self.seq_path.open("rb") as stream:
            return self._read_payload(stream, frame_index, validate=validate)

    def _read_payload(
        self, stream: io.BufferedReader, frame_index: int, *, validate: bool
    ) -> bytes:
        record = self.records[frame_index]
        offset = int(record["offset"])
        size = int(record["buffer_size"])
        stream.seek(offset)
        prefix_bytes = stream.read(4)
        if len(prefix_bytes) != 4:
            raise SeqFormatError(f"Missing length prefix at frame {frame_index}")
        prefix = struct.unpack("<I", prefix_bytes)[0]
        block = stream.read(size)
        trailing = stream.read(4)
        if len(block) != size or len(trailing) != 4:
            raise SeqFormatError(f"Truncated frame block at frame {frame_index}")
        jpeg = block[:-4]
        if validate:
            if prefix != size:
                raise SeqFormatError(
                    f"Length prefix mismatch at frame {frame_index}: {prefix} != {size}"
                )
            if not jpeg.startswith(b"\xff\xd8") or not jpeg.endswith(b"\xff\xd9"):
                raise SeqFormatError(f"Invalid JPEG boundaries at frame {frame_index}")
            width, height = jpeg_dimensions(jpeg)
            if (width, height) != (self.header.width, self.header.height):
                raise SeqFormatError(
                    f"JPEG dimensions {width}x{height} do not match header "
                    f"{self.header.width}x{self.header.height} at frame {frame_index}"
                )
            seconds = struct.unpack("<i", block[-4:])[0]
            milliseconds, microseconds = struct.unpack("<HH", trailing)
            expected = (
                int(record["seconds"]),
                int(record["milliseconds"]),
                int(record["microseconds"]),
            )
            if (seconds, milliseconds, microseconds) != expected:
                raise SeqFormatError(f"IDX/SEQ timestamp mismatch at frame {frame_index}")
        return jpeg

    def iter_jpeg_payloads(
        self, start: int = 0, count: int | None = None, *, validate: bool = True
    ) -> Iterator[bytes]:
        start, stop = self._normalized_range(start, count)
        with self.seq_path.open("rb", buffering=1024 * 1024) as stream:
            for frame_index in range(start, stop):
                yield self._read_payload(stream, frame_index, validate=validate)

    def validate_jpeg_samples(self, sample_count: int = 25) -> list[int]:
        count = min(max(sample_count, 1), self.frame_count)
        indices = np.linspace(0, self.frame_count - 1, count, dtype=np.int64)
        checked: list[int] = []
        with self.seq_path.open("rb", buffering=1024 * 1024) as stream:
            for index in np.unique(indices):
                frame_index = int(index)
                self._read_payload(stream, frame_index, validate=True)
                checked.append(frame_index)
        return checked

    def inspect(self, sample_count: int = 25) -> dict[str, object]:
        timestamps = self.timestamps_us
        deltas = np.diff(timestamps)
        if deltas.size:
            median_interval_us: float | None = float(np.median(deltas))
            min_interval_us: int | None = int(deltas.min())
            max_interval_us: int | None = int(deltas.max())
        else:
            median_interval_us = None
            min_interval_us = None
            max_interval_us = None
        return {
            "seq_path": str(self.seq_path.resolve()),
            "idx_path": str(self.idx_path.resolve()),
            "header": self.header.to_dict(),
            "index": self.validation.to_dict(),
            "timestamp": {
                "dtype": "int64",
                "unit": "unix_us",
                "first": int(timestamps[0]),
                "last": int(timestamps[-1]),
                "median_interval_us": median_interval_us,
                "min_interval_us": min_interval_us,
                "max_interval_us": max_interval_us,
            },
            "jpeg_samples_checked": self.validate_jpeg_samples(sample_count),
        }
