from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterator

from .compress import (
    compress_recording,
    verify_existing_package,
    verify_standalone_package,
)
from .ffmpeg_tools import FFmpegRuntime
from .naming import OutputPaths, output_paths
from .encoding import EncodingSettings
from .runtime_utils import atomic_write_json, copy_file_with_progress, sha256_file
from .seq_reader import SeqReader


FOLDER_MANIFEST_NAME = ".seqcomp_manifest.json"
DEFAULT_COMPRESSION_RATIO = 20.0
DEFAULT_ENCODING_TIME_RATIO = 1.2
GPU_COMPRESSION_RATIO = 10.0
GPU_ENCODING_TIME_RATIO = 0.25
TEMPORARY_MINIMUM_AGE_SECONDS = 24 * 60 * 60
_TEMPORARY_VIDEO_PATTERN = re.compile(
    r"^\.(?P<stem>.+)\.(?P<token>[a-z0-9_]{8})\.seqcomp\.tmp\.mkv$"
)


@dataclass(frozen=True)
class SeqPair:
    seq: Path
    idx: Path


@dataclass(frozen=True)
class FileSnapshot:
    size: int
    mtime_ns: int
    ctime_ns: int
    inode: int


@dataclass
class CompressionSummary:
    compressed: int = 0
    copied: int = 0
    skipped: int = 0
    deleted: int = 0
    failed: int = 0
    conflict_skipped: int = 0
    source_bytes: int = 0
    output_bytes: int = 0
    encoding_seconds: float = 0.0
    video_seconds: float = 0.0
    temporary_cleaned: int = 0
    temporary_kept: int = 0
    temporary_bytes_cleaned: int = 0

    @property
    def ok(self) -> bool:
        return self.failed == 0 and self.conflict_skipped == 0

    def to_dict(self) -> dict[str, int | float | bool]:
        return {**asdict(self), "ok": self.ok}


def format_gb(byte_count: float) -> str:
    """Format bytes as decimal gigabytes for CLI output."""
    return f"{byte_count / 1e9:,.2f} GB"


def format_duration(seconds: float) -> str:
    """Format seconds as a compact human-readable duration."""
    seconds = max(float(seconds), 0.0)
    if seconds >= 3600:
        rounded = int(round(seconds))
        hours, remainder = divmod(rounded, 3600)
        minutes, remaining = divmod(remainder, 60)
        return f"{hours} h {minutes} m {remaining} s"
    if seconds >= 60:
        minutes, remaining = divmod(int(round(seconds)), 60)
        return f"{minutes} m {remaining} s"
    return f"{seconds:.1f} s"


def iter_seq_pairs(folder: str | Path) -> Iterator[SeqPair]:
    root = Path(folder)
    if not root.is_dir():
        raise NotADirectoryError(root)
    for seq in sorted(root.rglob("*"), key=lambda path: str(path).lower()):
        if not seq.is_file() or seq.suffix.lower() != ".seq":
            continue
        idx = Path(f"{seq}.idx")
        if idx.is_file():
            yield SeqPair(seq, idx)


def _temporary_video_stem(path: Path) -> str | None:
    match = _TEMPORARY_VIDEO_PATTERN.fullmatch(path.name)
    return match.group("stem") if match else None


def _temporary_videos(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(
        (
            path
            for path in root.rglob("*.seqcomp.tmp.mkv")
            if path.is_file() and _temporary_video_stem(path) is not None
        ),
        key=lambda path: str(path).lower(),
    )


def _completed_outputs_for_temporary(path: Path) -> tuple[Path, OutputPaths]:
    stem = _temporary_video_stem(path)
    if stem is None:
        raise ValueError(f"Not a seqcomp temporary video: {path}")
    seq_path = path.with_name(f"{stem}.seq")
    return seq_path, output_paths(seq_path, path.parent)


def _verify_source_hashes_if_present(
    seq_path: Path,
    manifest: Mapping[str, object],
    *,
    show_progress: bool,
) -> tuple[bool, str]:
    idx_path = Path(f"{seq_path}.idx")
    if seq_path.is_file() != idx_path.is_file():
        return False, "only one of the source SEQ/IDX files exists"
    if not seq_path.is_file():
        return True, "source pair absent"
    source = manifest.get("source")
    if not isinstance(source, Mapping):
        return False, "manifest source section is invalid"
    if source.get("seq_sha256") != sha256_file(
        seq_path,
        show_progress=show_progress,
        description=f"Hash source {seq_path.name}",
    ):
        return False, "source SEQ SHA-256 mismatch"
    if source.get("idx_sha256") != sha256_file(
        idx_path,
        show_progress=show_progress,
        description=f"Hash source {idx_path.name}",
    ):
        return False, "source IDX SHA-256 mismatch"
    return True, "source hashes verified"


def _cleanup_temporary_videos(
    root: Path,
    runtime: FFmpegRuntime,
    *,
    dry_run: bool,
    show_progress: bool,
    current_time: float | None = None,
) -> tuple[int, int, int]:
    now = time.time() if current_time is None else current_time
    cleaned = 0
    kept = 0
    cleaned_bytes = 0
    temporary_files = _temporary_videos(root)
    if show_progress:
        print(
            f"[cleanup]  checking {len(temporary_files)} seqcomp temporary video(s)"
        )
    for temporary in temporary_files:
        relative = temporary.relative_to(root)
        try:
            initial = _snapshot(temporary)
        except OSError as exc:
            kept += 1
            if show_progress:
                print(f"[keep-temp] {relative}: cannot inspect temporary file: {exc}")
            continue
        age_seconds = max(now - initial.mtime_ns / 1_000_000_000, 0.0)
        if age_seconds < TEMPORARY_MINIMUM_AGE_SECONDS:
            kept += 1
            if show_progress:
                remaining = TEMPORARY_MINIMUM_AGE_SECONDS - age_seconds
                print(
                    f"[keep-temp] {relative}: newer than 24 h "
                    f"({format_duration(remaining)} remaining)"
                )
            continue
        seq_path, outputs = _completed_outputs_for_temporary(temporary)
        verified, reason, manifest = verify_standalone_package(
            outputs,
            runtime,
            show_progress=show_progress,
        )
        if not verified or manifest is None:
            kept += 1
            if show_progress:
                print(
                    f"[keep-temp] {relative}: completed package not verified: "
                    f"{reason}"
                )
            continue
        source_verified, source_reason = _verify_source_hashes_if_present(
            seq_path,
            manifest,
            show_progress=show_progress,
        )
        if not source_verified:
            kept += 1
            if show_progress:
                print(f"[keep-temp] {relative}: {source_reason}")
            continue
        try:
            unchanged = _snapshot(temporary) == initial
        except OSError as exc:
            kept += 1
            if show_progress:
                print(f"[keep-temp] {relative}: cannot recheck temporary file: {exc}")
            continue
        if not unchanged:
            kept += 1
            if show_progress:
                print(
                    f"[keep-temp] {relative}: temporary file changed during "
                    "verification"
                )
            continue
        size = initial.size
        if dry_run:
            cleaned += 1
            cleaned_bytes += size
            if show_progress:
                print(f"[clean-temp] {relative} ({format_gb(size)}, dry run)")
            continue
        try:
            temporary.unlink()
        except OSError as exc:
            kept += 1
            if show_progress:
                print(f"[keep-temp] {relative}: removal failed: {exc}")
            continue
        cleaned += 1
        cleaned_bytes += size
        if show_progress:
            print(f"[clean-temp] {relative} ({format_gb(size)} removed)")
    return cleaned, kept, cleaned_bytes


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _confirm(message: str, *, yes: bool, input_fn: Callable[[str], str]) -> bool:
    if yes:
        return True
    return input_fn(f"{message} [y/N] ").strip().lower() in {"y", "yes"}


def _snapshot(path: Path) -> FileSnapshot:
    stat = path.stat()
    return FileSnapshot(stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)


def _stable_sha256(
    path: Path, *, show_progress: bool = False, description: str | None = None
) -> tuple[str, FileSnapshot]:
    before = _snapshot(path)
    digest = sha256_file(
        path, show_progress=show_progress, description=description
    )
    after = _snapshot(path)
    if before != after:
        raise RuntimeError(f"Source changed while hashing: {path}")
    return digest, after


def _delete_verified_pair(
    pair: SeqPair,
    seq_sha256: str,
    idx_sha256: str,
    *,
    show_progress: bool,
) -> None:
    """Rehash and immediately delete one verified SEQ/IDX source pair."""
    seq_hash, seq_snapshot = _stable_sha256(
        pair.seq,
        show_progress=show_progress,
        description=f"Rehash {pair.seq.name}",
    )
    idx_hash, idx_snapshot = _stable_sha256(
        pair.idx,
        show_progress=show_progress,
        description=f"Rehash {pair.idx.name}",
    )
    if seq_hash != seq_sha256:
        raise RuntimeError(f"Source SEQ changed after compression: {pair.seq}")
    if idx_hash != idx_sha256:
        raise RuntimeError(f"Source IDX changed after compression: {pair.idx}")
    if _snapshot(pair.seq) != seq_snapshot:
        raise RuntimeError(f"Source SEQ changed after final hash: {pair.seq}")
    if _snapshot(pair.idx) != idx_snapshot:
        raise RuntimeError(f"Source IDX changed after final hash: {pair.idx}")
    pair.seq.unlink()
    pair.idx.unlink()


def _copy_verified(
    source: Path,
    destination: Path,
    *,
    force: bool,
    yes: bool,
    dry_run: bool,
    show_progress: bool,
    input_fn: Callable[[str], str],
) -> tuple[str, dict[str, object]]:
    source_hash = sha256_file(
        source,
        show_progress=show_progress,
        description=f"Hash source {source.name}",
    )
    record: dict[str, object] = {
        "source_sha256": source_hash,
        "bytes": source.stat().st_size,
    }
    if destination.exists():
        if destination.is_file() and sha256_file(
            destination,
            show_progress=show_progress,
            description=f"Hash existing {destination.name}",
        ) == source_hash:
            record["destination_sha256"] = source_hash
            return "skipped", record
        if not (force or _confirm(f"Replace conflicting file {destination}?", yes=yes, input_fn=input_fn)):
            return "conflict", record
    if dry_run:
        return "copied", record
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{destination.name}.",
        suffix=".seqcomp-copy.tmp",
        dir=destination.parent,
        delete=False,
    ) as temporary_stream:
        temporary = Path(temporary_stream.name)
    try:
        copy_file_with_progress(
            source,
            temporary,
            show_progress=show_progress,
            description=f"Copy {source.name}",
        )
        copied_hash = sha256_file(
            temporary,
            show_progress=show_progress,
            description=f"Verify copy {source.name}",
        )
        if copied_hash != source_hash:
            raise RuntimeError(f"SHA-256 mismatch while copying {source}")
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    record["destination_sha256"] = copied_hash
    return "copied", record


def _other_files(root: Path, pairs: list[SeqPair]) -> Iterator[Path]:
    excluded: set[Path] = {root / FOLDER_MANIFEST_NAME}
    for pair in pairs:
        excluded.update((pair.seq, pair.idx))
        outputs = output_paths(pair.seq, pair.seq.parent)
        excluded.update((outputs.video, outputs.timestamps, outputs.manifest))
    for path in sorted(root.rglob("*"), key=lambda item: str(item).lower()):
        if path.is_file() and path not in excluded and path.name != FOLDER_MANIFEST_NAME:
            yield path


def _space_target(path: Path) -> Path:
    candidate = path
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return candidate


def _same_volume(first: Path, second: Path) -> bool:
    return os.stat(_space_target(first)).st_dev == os.stat(_space_target(second)).st_dev


def _require_space(
    root: Path,
    destination_root: Path | None,
    pairs: list[SeqPair],
    *,
    is_file: bool,
    delete: bool,
) -> None:
    # A conservative 2x compression floor plus 16 MiB package overhead.
    package_requirements = [
        (pair.seq.stat().st_size + pair.idx.stat().st_size) // 2 + 16 * 1024 * 1024
        for pair in pairs
    ]
    target = destination_root if destination_root is not None else root
    if delete and _same_volume(root, target):
        required = max(package_requirements, default=0)
    else:
        required = sum(package_requirements)
    if destination_root is not None and not is_file:
        required += sum(path.stat().st_size for path in _other_files(root, pairs))
    free = shutil.disk_usage(_space_target(target)).free
    if free < required:
        raise OSError(
            f"Insufficient free space at {target}: need approximately "
            f"{format_gb(required)}, have {format_gb(free)}"
        )


def compress_path(
    input_path: str | Path,
    runtime: FFmpegRuntime,
    settings: EncodingSettings,
    *,
    dest: str | Path | None = None,
    delete: bool = False,
    dry_run: bool = False,
    force: bool = False,
    yes: bool = False,
    quiet: bool = False,
    cleanup_temp: bool = False,
    input_fn: Callable[[str], str] = input,
) -> CompressionSummary:
    source = Path(input_path).resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    is_file = source.is_file()
    if is_file and source.suffix.lower() != ".seq":
        raise ValueError(f"Expected a .seq file or folder, got: {source}")
    root = source.parent if is_file else source
    destination_root = Path(dest).resolve() if dest is not None else None
    if destination_root is not None:
        if not is_file and (
            destination_root == root
            or _is_within(destination_root, root)
            or _is_within(root, destination_root)
        ):
            raise ValueError("Source and destination folders must not contain each other")
        if destination_root.exists() and any(destination_root.iterdir()):
            if not _confirm(f"Destination {destination_root} is not empty; continue?", yes=yes, input_fn=input_fn):
                raise RuntimeError("Destination confirmation declined")
    if not quiet:
        print(f"[scan]     {source}")
    pairs = [SeqPair(source, Path(f"{source}.idx"))] if is_file else list(iter_seq_pairs(root))
    if is_file and not pairs[0].idx.is_file():
        raise FileNotFoundError(pairs[0].idx)
    summary = CompressionSummary()
    if cleanup_temp:
        cleanup_root = destination_root if destination_root is not None else root
        cleaned, kept, cleaned_bytes = _cleanup_temporary_videos(
            cleanup_root,
            runtime,
            dry_run=dry_run,
            show_progress=not quiet,
        )
        summary.temporary_cleaned = cleaned
        summary.temporary_kept = kept
        summary.temporary_bytes_cleaned = cleaned_bytes
    if not pairs:
        if cleanup_temp:
            return summary
        raise ValueError(f"No .seq + .seq.idx pairs found under {source}")
    if not dry_run:
        if not quiet:
            print(f"[space]    checking free space for {len(pairs)} recording(s)")
        _require_space(
            root,
            destination_root,
            pairs,
            is_file=is_file,
            delete=delete,
        )

    recordings: dict[str, object] = {}
    copy_records: dict[str, object] = {}
    for pair in pairs:
        relative = pair.seq.relative_to(root) if not is_file else Path(pair.seq.name)
        output_dir = (
            destination_root / relative.parent
            if destination_root is not None
            else pair.seq.parent
        )
        if not quiet:
            print(f"[check]    {relative}")
        try:
            verified, reason, outputs = verify_existing_package(
                pair.seq,
                output_dir,
                runtime,
                settings,
                show_progress=not quiet,
            )
        except Exception as exc:
            summary.failed += 1
            if not quiet:
                print(f"[failed]   {relative}: validation error: {exc}")
            continue
        compressed_now = False
        if verified and not force:
            summary.skipped += 1
            if not quiet:
                print(f"[skip]     {relative} (existing package verified)")
        else:
            exists = any(path.exists() for path in (outputs.video, outputs.timestamps, outputs.manifest))
            if exists and not (force or _confirm(f"Replace unverified output for {relative} ({reason})?", yes=yes, input_fn=input_fn)):
                summary.conflict_skipped += 1
                if not quiet:
                    print(f"[conflict] {relative} ({reason})")
                continue
            if dry_run:
                summary.compressed += 1
                if not quiet:
                    source_bytes = pair.seq.stat().st_size + pair.idx.stat().st_size
                    print(
                        f"[compress] {relative} ({format_gb(source_bytes)}, dry run)"
                    )
                continue
            try:
                if not quiet:
                    source_bytes = pair.seq.stat().st_size + pair.idx.stat().st_size
                    print(f"[compress] {relative} ({format_gb(source_bytes)})")
                compress_recording(
                    pair.seq,
                    output_dir,
                    runtime,
                    settings,
                    overwrite=exists or force,
                    show_progress=not quiet,
                )
                outputs = output_paths(pair.seq, output_dir)
                summary.compressed += 1
                compressed_now = True
            except Exception as exc:
                summary.failed += 1
                if not quiet:
                    print(f"[failed]   {relative}: {exc}")
                continue
        try:
            manifest = json.loads(outputs.manifest.read_text(encoding="utf-8"))
            source_seq_sha256 = manifest["source"]["seq_sha256"]
            source_idx_sha256 = manifest["source"]["idx_sha256"]
            recordings[relative.as_posix()] = {
                "source_seq_sha256": source_seq_sha256,
                "source_idx_sha256": source_idx_sha256,
                "video_sha256": manifest["video"]["video_sha256"],
                "timestamps_sha256": manifest["timestamps"]["timestamps_sha256"],
                "parameters": manifest["encoding"],
            }
            if compressed_now:
                compression = manifest["compression"]
                encode = manifest["encode"]
                source_bytes = int(compression["source_package_bytes"])
                output_bytes = int(
                    compression["output_video_and_timestamps_bytes"]
                )
                video_seconds = float(compression["source_duration_seconds"])
                encoding_seconds = float(encode["wall_seconds"])
                summary.source_bytes += source_bytes
                summary.output_bytes += output_bytes
                summary.video_seconds += video_seconds
                summary.encoding_seconds += encoding_seconds
                if not quiet:
                    ratio = source_bytes / output_bytes if output_bytes else float("inf")
                    time_ratio = (
                        encoding_seconds / video_seconds if video_seconds else 0.0
                    )
                    print(
                        f"[done]     {relative}: {format_gb(source_bytes)} -> "
                        f"{format_gb(output_bytes)} ({ratio:.1f}x, "
                        f"{time_ratio:.2f}x video time)"
                    )
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            summary.failed += 1
            if not quiet:
                print(f"[failed]   {relative}: manifest error: {exc}")
            continue
        if delete:
            try:
                if not quiet:
                    print(f"[verify]   rehashing {relative} before deletion")
                _delete_verified_pair(
                    pair,
                    source_seq_sha256,
                    source_idx_sha256,
                    show_progress=not quiet,
                )
                summary.deleted += 1
                if not quiet:
                    print(f"[delete]   {relative} + {pair.idx.name}")
            except Exception as exc:
                summary.failed += 1
                if not quiet:
                    print(f"[keep]     {relative}: deletion cancelled: {exc}")

    if destination_root is not None and not is_file:
        if not dry_run:
            for directory in sorted((path for path in root.rglob("*") if path.is_dir())):
                (destination_root / directory.relative_to(root)).mkdir(parents=True, exist_ok=True)
        for other in _other_files(root, pairs):
            relative = other.relative_to(root)
            try:
                if not quiet:
                    print(f"[check]    {relative} (ordinary file)")
                action, record = _copy_verified(
                    other,
                    destination_root / relative,
                    force=force,
                    yes=yes,
                    dry_run=dry_run,
                    show_progress=not quiet,
                    input_fn=input_fn,
                )
                copy_records[relative.as_posix()] = record
                if action == "copied":
                    summary.copied += 1
                    if not quiet:
                        print(f"[copy]     {relative} ({format_gb(other.stat().st_size)})")
                elif action == "skipped":
                    summary.skipped += 1
                    if not quiet:
                        print(f"[skip]     {relative} (identical copy exists)")
                else:
                    summary.conflict_skipped += 1
                    if not quiet:
                        print(f"[conflict] {relative}")
            except Exception as exc:
                summary.failed += 1
                if not quiet:
                    print(f"[failed]   {relative}: copy error: {exc}")

    if not dry_run:
        if not is_file or destination_root is not None:
            manifest_root = destination_root if destination_root is not None else root
            manifest_root.mkdir(parents=True, exist_ok=True)
            atomic_write_json(
                manifest_root / FOLDER_MANIFEST_NAME,
                {
                    "schema_version": 1,
                    "source_root": str(root),
                    "recordings": recordings,
                    "other_files": copy_records,
                },
            )
    return summary


def status_path(input_path: str | Path) -> list[dict[str, object]]:
    """Return machine-readable recording states and exact byte counts."""
    source = Path(input_path).resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    is_file = source.is_file()
    pairs = [SeqPair(source, Path(f"{source}.idx"))] if is_file else list(iter_seq_pairs(source))
    if is_file and not pairs[0].idx.is_file():
        raise FileNotFoundError(pairs[0].idx)
    root = source.parent if source.is_file() else source
    raw_by_base = {
        (pair.seq.relative_to(root) if not is_file else Path(pair.seq.name)).as_posix()[: -len(".seq")]: pair
        for pair in pairs
    }
    output_bases: set[str] = set()
    if not is_file:
        suffixes = (".mkv", ".timestamps.npy", ".manifest.json")
        for path in root.rglob("*"):
            if not path.is_file() or path.name == FOLDER_MANIFEST_NAME:
                continue
            if _temporary_video_stem(path) is not None:
                continue
            relative = path.relative_to(root).as_posix()
            for suffix in suffixes:
                if relative.endswith(suffix):
                    output_bases.add(relative[: -len(suffix)])
                    break
    rows: list[dict[str, object]] = []
    for base in sorted(set(raw_by_base) | output_bases):
        pair = raw_by_base.get(base)
        output_root = root / Path(base).parent
        output_seq = root / f"{base}.seq"
        outputs = output_paths(output_seq, output_root)
        existing = [path.is_file() for path in (outputs.video, outputs.timestamps, outputs.manifest)]
        has_raw = pair is not None
        if has_raw and all(existing):
            state = "both"
        elif has_raw and not any(existing):
            state = "raw-only"
        elif not has_raw and all(existing):
            state = "compressed-only"
        else:
            state = "incomplete"
        source_bytes = (
            pair.seq.stat().st_size + pair.idx.stat().st_size if pair else 0
        )
        duration_seconds: float | None = None
        source_size_from = "raw" if pair else "unknown"
        error: str | None = None
        if pair:
            try:
                reader = SeqReader(pair.seq)
                duration_seconds = reader.frame_count / reader.header.frame_rate
            except Exception as exc:
                error = str(exc)
        if outputs.manifest.is_file():
            try:
                manifest = json.loads(outputs.manifest.read_text(encoding="utf-8"))
                if isinstance(manifest, Mapping):
                    compression = manifest.get("compression")
                    if isinstance(compression, Mapping):
                        if not source_bytes:
                            source_bytes = int(
                                compression.get("source_package_bytes", 0)
                            )
                            if source_bytes:
                                source_size_from = "manifest"
                        if duration_seconds is None:
                            recorded_duration = compression.get(
                                "source_duration_seconds"
                            )
                            if recorded_duration is not None:
                                duration_seconds = float(recorded_duration)
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                pass
        rows.append(
            {
                "seq": f"{base}.seq",
                "state": state,
                "source_bytes": source_bytes,
                "source_size_from": source_size_from,
                "output_bytes": sum(
                    path.stat().st_size
                    for path in (outputs.video, outputs.timestamps)
                    if path.is_file()
                ),
                "video_duration_seconds": duration_seconds,
                "error": error,
            }
        )
    return rows


def status_report(
    input_path: str | Path,
    compression_ratio: float = DEFAULT_COMPRESSION_RATIO,
    encoding_time_ratio: float = DEFAULT_ENCODING_TIME_RATIO,
) -> str:
    """Return a human-readable folder summary with actual and estimated totals."""
    if compression_ratio <= 0:
        raise ValueError("compression ratio must be greater than zero")
    if encoding_time_ratio <= 0:
        raise ValueError("encoding time ratio must be greater than zero")
    source = Path(input_path).resolve()
    rows = status_path(source)
    temporary_files = _temporary_videos(source) if source.is_dir() else []
    lines = [
        f"Status: {source}",
        (
            f"Estimates: {compression_ratio:g}x compression, "
            f"{encoding_time_ratio:g}x video duration for encoding"
        ),
    ]
    counts = {
        "raw-only": 0,
        "both": 0,
        "compressed-only": 0,
        "incomplete": 0,
    }
    known_original_bytes = 0.0
    projected_output_bytes = 0.0
    remaining_source_bytes = 0.0
    remaining_output_bytes = 0.0
    remaining_duration_seconds = 0.0

    for row in rows:
        state = str(row["state"])
        counts[state] += 1
        source_bytes = float(row["source_bytes"])
        output_bytes = float(row["output_bytes"])
        duration = row["video_duration_seconds"]
        duration_seconds = float(duration) if duration is not None else 0.0
        lines.append("")
        lines.append(f"[{state}] {row['seq']}")

        if state == "raw-only":
            estimated_output = source_bytes / compression_ratio
            estimated_saving = source_bytes - estimated_output
            estimated_time = duration_seconds * encoding_time_ratio
            known_original_bytes += source_bytes
            projected_output_bytes += estimated_output
            remaining_source_bytes += source_bytes
            remaining_output_bytes += estimated_output
            remaining_duration_seconds += duration_seconds
            lines.append(
                f"  Source {format_gb(source_bytes)} -> estimated output "
                f"{format_gb(estimated_output)}"
            )
            lines.append(
                f"  Estimated saving {format_gb(estimated_saving)} | "
                f"video {format_duration(duration_seconds)} | "
                f"encoding ~{format_duration(estimated_time)}"
            )
        elif state in {"both", "compressed-only"}:
            if source_bytes:
                saved = source_bytes - output_bytes
                ratio = source_bytes / output_bytes if output_bytes else float("inf")
                known_original_bytes += source_bytes
                projected_output_bytes += output_bytes
                lines.append(
                    f"  Source {format_gb(source_bytes)} -> output "
                    f"{format_gb(output_bytes)} ({ratio:.1f}x)"
                )
                lines.append(f"  Saved {format_gb(saved)}")
            else:
                lines.append(f"  Output {format_gb(output_bytes)} (source size unknown)")
        else:
            lines.append(
                f"  Source {format_gb(source_bytes)} | partial output "
                f"{format_gb(output_bytes)}"
            )
        if row["error"]:
            lines.append(f"  Warning: {row['error']}")

    total_count = len(rows)
    total_saving = known_original_bytes - projected_output_bytes
    overall_ratio = (
        known_original_bytes / projected_output_bytes
        if projected_output_bytes
        else 0.0
    )
    remaining_saving = remaining_source_bytes - remaining_output_bytes
    lines.extend(
        [
            "",
            "Summary",
            (
                f"  Recordings: {total_count} | raw-only {counts['raw-only']} | "
                f"both {counts['both']} | compressed-only "
                f"{counts['compressed-only']} | incomplete {counts['incomplete']}"
            ),
            (
                f"  Original data represented: {format_gb(known_original_bytes)} | "
                f"compressed/projected: {format_gb(projected_output_bytes)}"
            ),
            (
                f"  Total space saving: {format_gb(total_saving)}"
                + (f" ({overall_ratio:.1f}x overall)" if overall_ratio else "")
            ),
            (
                f"  Remaining raw-only: {format_gb(remaining_source_bytes)} -> "
                f"~{format_gb(remaining_output_bytes)} | saving "
                f"~{format_gb(remaining_saving)}"
            ),
            (
                f"  Remaining video: {format_duration(remaining_duration_seconds)} | "
                f"estimated encoding: "
                f"~{format_duration(remaining_duration_seconds * encoding_time_ratio)}"
            ),
        ]
    )
    if temporary_files:
        temporary_bytes = sum(path.stat().st_size for path in temporary_files)
        lines.extend(
            [
                "",
                "Temporary files",
                (
                    f"  Detected: {len(temporary_files)} | "
                    f"size {format_gb(temporary_bytes)}"
                ),
                (
                    "  These files are excluded from recording counts. Run "
                    f"seqcomp compress \"{source}\" --cleanup-temp to verify "
                    "and remove stale files."
                ),
            ]
        )
    return "\n".join(lines)
