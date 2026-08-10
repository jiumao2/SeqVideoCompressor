from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import pytest

from conftest import create_test_seq
from seqcomp.seq_reader import SeqFormatError, SeqReader


def test_reader_parses_header_index_payload_and_timestamps(tiny_seq: Path) -> None:
    reader = SeqReader(tiny_seq)
    assert reader.header.width == 31
    assert reader.header.height == 29
    assert reader.header.frame_rate == 100.0
    assert reader.frame_count == 6
    assert reader.validation.final_end == tiny_seq.stat().st_size
    timestamps = reader.timestamps_us
    assert timestamps.dtype == np.int64
    assert np.all(np.diff(timestamps) > 0)
    payloads = list(reader.iter_jpeg_payloads())
    assert len(payloads) == 6
    assert all(payload.startswith(b"\xff\xd8") for payload in payloads)
    assert all(payload.endswith(b"\xff\xd9") for payload in payloads)


def test_truncated_seq_is_rejected(tiny_seq: Path) -> None:
    data = tiny_seq.read_bytes()
    tiny_seq.write_bytes(data[:-1])
    with pytest.raises(SeqFormatError, match="does not match SEQ size"):
        SeqReader(tiny_seq)


def test_discontinuous_idx_is_rejected(tiny_seq: Path) -> None:
    idx_path = Path(f"{tiny_seq}.idx")
    data = bytearray(idx_path.read_bytes())
    second_offset = struct.unpack_from("<Q", data, 24)[0]
    struct.pack_into("<Q", data, 24, second_offset + 1)
    idx_path.write_bytes(data)
    with pytest.raises(SeqFormatError, match="not contiguous"):
        SeqReader(tiny_seq)


def test_nonmonotonic_timestamp_is_rejected(tiny_seq: Path) -> None:
    idx_path = Path(f"{tiny_seq}.idx")
    data = bytearray(idx_path.read_bytes())
    first_seconds = struct.unpack_from("<i", data, 12)[0]
    struct.pack_into("<iHH", data, 24 + 12, first_seconds, 100, 7)
    idx_path.write_bytes(data)
    with pytest.raises(SeqFormatError, match="not strictly increasing"):
        SeqReader(tiny_seq)


def test_bad_length_prefix_is_rejected_on_payload_read(tiny_seq: Path) -> None:
    data = bytearray(tiny_seq.read_bytes())
    original = struct.unpack_from("<I", data, 1024)[0]
    struct.pack_into("<I", data, 1024, original + 1)
    tiny_seq.write_bytes(data)
    reader = SeqReader(tiny_seq)
    with pytest.raises(SeqFormatError, match="Length prefix mismatch"):
        reader.read_jpeg_payload(0)


def test_bad_jpeg_boundary_is_rejected(tiny_seq: Path) -> None:
    data = bytearray(tiny_seq.read_bytes())
    data[1028:1030] = b"NO"
    tiny_seq.write_bytes(data)
    reader = SeqReader(tiny_seq)
    with pytest.raises(SeqFormatError, match="Invalid JPEG boundaries"):
        reader.read_jpeg_payload(0)


def test_jpeg_dimensions_must_match_header(tiny_seq: Path) -> None:
    data = bytearray(tiny_seq.read_bytes())
    struct.pack_into("<I", data, 548, 32)
    tiny_seq.write_bytes(data)
    reader = SeqReader(tiny_seq)
    with pytest.raises(SeqFormatError, match="JPEG dimensions 31x29.*header 32x29"):
        reader.read_jpeg_payload(0)


def test_one_frame_inspect_has_null_interval_statistics(tmp_path: Path) -> None:
    seq = create_test_seq(tmp_path, frame_count=1)
    result = SeqReader(seq).inspect()
    assert result["timestamp"]["median_interval_us"] is None
    assert result["timestamp"]["min_interval_us"] is None
    assert result["timestamp"]["max_interval_us"] is None
    assert result["jpeg_samples_checked"] == [0]
