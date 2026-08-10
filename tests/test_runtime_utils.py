from __future__ import annotations

import hashlib
from pathlib import Path

import seqcomp.runtime_utils as runtime_utils


class RecordingProgress:
    instances = []

    def __init__(self, **kwargs):
        self.total = kwargs["total"]
        self.n = 0
        self.closed = False
        self.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def update(self, amount: int) -> None:
        self.n += amount

    def close(self) -> None:
        self.closed = True


def test_hash_and_copy_progress_track_bytes(tmp_path: Path, monkeypatch) -> None:
    RecordingProgress.instances = []
    monkeypatch.setattr(runtime_utils, "PROGRESS_MIN_BYTES", 0)
    monkeypatch.setattr(runtime_utils, "tqdm", RecordingProgress)
    source = tmp_path / "source.bin"
    destination = tmp_path / "destination.bin"
    data = b"seqcomp-progress" * 1024
    source.write_bytes(data)

    digest = runtime_utils.sha256_file(source, show_progress=True)
    runtime_utils.copy_file_with_progress(
        source, destination, show_progress=True
    )

    assert digest == hashlib.sha256(data).hexdigest()
    assert destination.read_bytes() == data
    assert len(RecordingProgress.instances) == 2
    assert all(item.total == len(data) for item in RecordingProgress.instances)
    assert all(item.n == len(data) for item in RecordingProgress.instances)
    assert all(item.closed for item in RecordingProgress.instances)
