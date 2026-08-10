from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import sys
import threading
import time
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import psutil
from tqdm.auto import tqdm


PROGRESS_MIN_BYTES = 16 * 1024 * 1024


def sha256_file(
    path: str | Path,
    chunk_size: int = 8 * 1024 * 1024,
    *,
    show_progress: bool = False,
    description: str | None = None,
) -> str:
    source = Path(path)
    size = source.stat().st_size
    digest = hashlib.sha256()
    with source.open("rb") as stream, tqdm(
        total=size,
        desc=description or f"SHA-256 {source.name}",
        unit="B",
        unit_scale=True,
        unit_divisor=1000,
        dynamic_ncols=True,
        mininterval=0.5,
        disable=not show_progress or size < PROGRESS_MIN_BYTES,
    ) as progress:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
            progress.update(len(chunk))
    return digest.hexdigest()


def copy_file_with_progress(
    source: str | Path,
    destination: str | Path,
    *,
    show_progress: bool = False,
    description: str | None = None,
    chunk_size: int = 8 * 1024 * 1024,
) -> None:
    """Copy a file with metadata and a byte progress bar for large files."""
    source_path = Path(source)
    destination_path = Path(destination)
    size = source_path.stat().st_size
    with source_path.open("rb") as reader, destination_path.open("wb") as writer, tqdm(
        total=size,
        desc=description or f"Copy {source_path.name}",
        unit="B",
        unit_scale=True,
        unit_divisor=1000,
        dynamic_ncols=True,
        mininterval=0.5,
        disable=not show_progress or size < PROGRESS_MIN_BYTES,
    ) as progress:
        while chunk := reader.read(chunk_size):
            writer.write(chunk)
            progress.update(len(chunk))
    shutil.copystat(source_path, destination_path)


def atomic_save_npy(path: str | Path, values: np.ndarray) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+b",
            prefix=f".{destination.name}.",
            suffix=".seqcomp.tmp",
            dir=destination.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            np.save(stream, values, allow_pickle=False)
        os.replace(temporary, destination)
    except BaseException:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def atomic_write_json(path: str | Path, data: object) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{destination.name}.",
            suffix=".seqcomp.tmp",
            dir=destination.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, destination)
    except BaseException:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def environment_details() -> dict[str, object]:
    import cv2

    return {
        "python": sys.version,
        "python_executable": sys.executable,
        "conda_prefix": os.environ.get("CONDA_PREFIX"),
        "platform": platform.platform(),
        "processor": platform.processor(),
        "logical_cpu_count": psutil.cpu_count(logical=True),
        "physical_cpu_count": psutil.cpu_count(logical=False),
        "memory_bytes": psutil.virtual_memory().total,
        "numpy": np.__version__,
        "opencv": cv2.__version__,
    }


@dataclass
class ProcessUsage:
    peak_rss_bytes: int = 0
    cpu_seconds: float = 0.0


class ProcessMonitor:
    """Sample a process tree while a subprocess is alive."""

    def __init__(self, pid: int, interval: float = 0.1):
        self.pid = pid
        self.interval = interval
        self.usage = ProcessUsage()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> ProcessUsage:
        self._stop.set()
        self._thread.join(timeout=max(1.0, self.interval * 5))
        return self.usage

    def _run(self) -> None:
        try:
            root = psutil.Process(self.pid)
        except psutil.Error:
            return
        while not self._stop.is_set():
            rss = 0
            cpu = 0.0
            processes = [root]
            try:
                processes.extend(root.children(recursive=True))
            except psutil.Error:
                pass
            for process in processes:
                try:
                    rss += process.memory_info().rss
                    times = process.cpu_times()
                    cpu += times.user + times.system
                except psutil.Error:
                    continue
            self.usage.peak_rss_bytes = max(self.usage.peak_rss_bytes, rss)
            self.usage.cpu_seconds = max(self.usage.cpu_seconds, cpu)
            self._stop.wait(self.interval)


def utc_run_id() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
