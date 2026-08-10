from __future__ import annotations

import os
from pathlib import Path

import pytest

from seqcomp.seq_reader import SeqReader


@pytest.mark.realdata
def test_real_seq_structure_when_configured() -> None:
    value = os.environ.get("SEQCOMP_TEST_SEQ")
    if not value:
        pytest.skip("Set SEQCOMP_TEST_SEQ to run the real-data test")
    reader = SeqReader(Path(value))
    inspection = reader.inspect(25)
    assert inspection["index"]["frame_count"] > 0
    assert inspection["index"]["final_end"] == inspection["index"]["seq_size"]
