from __future__ import annotations

import struct
from pathlib import Path

import cv2
import numpy as np
import pytest


def create_test_seq(
    root: Path,
    *,
    name: str = "test-recording.000.seq",
    width: int = 31,
    height: int = 29,
    frame_count: int = 6,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    seq_path = root / name
    idx_path = Path(f"{seq_path}.idx")
    header = bytearray(1024)
    struct.pack_into("<i", header, 28, 5)
    struct.pack_into("<i", header, 32, 1024)
    struct.pack_into("<6I", header, 548, width, height, 8, 8, width * height, 100)
    struct.pack_into("<H", header, 572, frame_count % 65536)
    struct.pack_into("<I", header, 576, 0)
    struct.pack_into("<I", header, 580, width * height)
    struct.pack_into("<d", header, 584, 100.0)
    struct.pack_into("<I", header, 592, 0)
    header[620] = 1
    seq_data = bytearray(header)
    idx_data = bytearray()
    offset = 1024
    rng = np.random.default_rng(42)
    for index in range(frame_count):
        y, x = np.mgrid[:height, :width]
        frame = ((x * 3 + y * 5 + index * 11) % 256).astype(np.uint8)
        frame ^= rng.integers(0, 4, size=frame.shape, dtype=np.uint8)
        ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
        assert ok
        jpeg = encoded.tobytes()
        seconds = 1_700_000_000
        milliseconds = 100 + index * 10
        microseconds = 7 + index
        buffer_size = len(jpeg) + 4
        idx_data += struct.pack(
            "<QIiHHI",
            offset,
            buffer_size,
            seconds,
            milliseconds,
            microseconds,
            0,
        )
        seq_data += struct.pack("<I", buffer_size)
        seq_data += jpeg
        seq_data += struct.pack("<iHH", seconds, milliseconds, microseconds)
        offset += buffer_size + 8
    seq_path.write_bytes(seq_data)
    idx_path.write_bytes(idx_data)
    return seq_path


@pytest.fixture
def tiny_seq(tmp_path: Path) -> Path:
    return create_test_seq(tmp_path)
