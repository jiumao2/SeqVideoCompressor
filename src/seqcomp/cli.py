from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .core import (
    DEFAULT_COMPRESSION_RATIO,
    DEFAULT_ENCODING_TIME_RATIO,
    GPU_COMPRESSION_RATIO,
    GPU_ENCODING_TIME_RATIO,
    CompressionSummary,
    compress_path,
    format_duration,
    format_gb,
    status_report,
)
from .encoding import CODEC_NAMES, GPU_PRESETS, PRESETS, make_settings
from .ffmpeg_tools import inspect_ffmpeg, inspect_nvenc
from .seq_reader import SeqReader


def _json_print(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _bounded_int(name: str, minimum: int, maximum: int | None = None):
    def convert(value: str) -> int:
        number = int(value)
        if number < minimum or (maximum is not None and number > maximum):
            limit = f"{minimum}..{maximum}" if maximum is not None else f">= {minimum}"
            raise argparse.ArgumentTypeError(f"{name} must be {limit}")
        return number

    return convert


def _positive_float(name: str):
    def convert(value: str) -> float:
        number = float(value)
        if number <= 0:
            raise argparse.ArgumentTypeError(f"{name} must be greater than zero")
        return number

    return convert


def _add_codec_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--codec",
        choices=tuple(CODEC_NAMES),
        default="h265",
        help="video codec (default: h265)",
    )
    parser.add_argument(
        "--crf",
        type=_bounded_int("CRF", 1, 51),
        default=None,
        help="CPU quality level; lower is higher quality/larger (default: 18)",
    )
    parser.add_argument(
        "--preset",
        choices=PRESETS,
        default=None,
        help="CPU encoding effort/speed tradeoff (default: medium)",
    )
    parser.add_argument(
        "--keyint",
        type=_bounded_int("keyint", 1),
        default=250,
        help="maximum keyframe interval in frames (default: 250)",
    )
    parser.add_argument(
        "--gpu",
        action="store_true",
        help="use NVIDIA H.265 NVENC instead of CPU encoding",
    )
    parser.add_argument(
        "--cq",
        type=_bounded_int("CQ", 1, 51),
        default=None,
        help="NVENC quality level; lower is higher quality/larger (default: 28)",
    )
    parser.add_argument(
        "--gpu-preset",
        choices=GPU_PRESETS,
        default=None,
        help="NVENC encoding effort preset (default: p5)",
    )
    parser.add_argument(
        "--gpu-device",
        type=_bounded_int("GPU device", 0),
        default=None,
        metavar="INDEX",
        help="zero-based NVIDIA GPU index (default: automatic)",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seqcomp",
        description="Validated NorPix SEQ folder compressor and backup tool",
    )
    parser.add_argument("--version", action="version", version=f"seqcomp {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser(
        "status", help="summarize compression state, space savings, and estimated time"
    )
    status_parser.add_argument("input", type=Path)
    status_parser.add_argument(
        "--gpu",
        action="store_true",
        help="estimate NVIDIA NVENC compression and verify GPU support",
    )
    status_parser.add_argument(
        "--gpu-device",
        type=_bounded_int("GPU device", 0),
        default=None,
        metavar="INDEX",
        help="zero-based NVIDIA GPU index (default: automatic)",
    )
    status_parser.add_argument(
        "--ratio",
        type=_positive_float("ratio"),
        default=None,
        help=(
            "assumed compression ratio for raw-only recordings; "
            f"defaults to {DEFAULT_COMPRESSION_RATIO:g} for CPU or "
            f"{GPU_COMPRESSION_RATIO:g} for GPU"
        ),
    )
    status_parser.add_argument(
        "--time-ratio",
        type=_positive_float("time ratio"),
        default=None,
        help=(
            "assumed encoding time divided by video duration; "
            f"defaults to {DEFAULT_ENCODING_TIME_RATIO:g} for CPU or "
            f"{GPU_ENCODING_TIME_RATIO:g} for GPU"
        ),
    )

    inspect_parser = subparsers.add_parser("inspect", help="validate SEQ/IDX metadata")
    inspect_parser.add_argument("input", type=Path)
    inspect_parser.add_argument("--samples", type=_bounded_int("samples", 1), default=25)

    compress_parser = subparsers.add_parser(
        "compress", help="compress one SEQ pair or recursively process a folder"
    )
    compress_parser.add_argument("input", type=Path, help="source .seq file or folder")
    compress_parser.add_argument(
        "--dest", type=Path, help="mirror-backup destination (default: in place)"
    )
    _add_codec_arguments(compress_parser)
    compress_parser.add_argument(
        "--delete",
        action="store_true",
        help="delete each source SEQ/IDX pair immediately after verification",
    )
    compress_parser.add_argument(
        "--dry-run", action="store_true", help="show planned actions without writing"
    )
    compress_parser.add_argument(
        "--force", action="store_true", help="recompress verified existing outputs"
    )
    compress_parser.add_argument(
        "--yes", action="store_true", help="accept destination/conflict prompts"
    )
    compress_parser.add_argument(
        "--quiet",
        action="store_true",
        help="hide progress bars and per-file messages",
    )

    return parser


def _print_compression_summary(
    summary: CompressionSummary, *, dry_run: bool
) -> None:
    if dry_run:
        print(
            f"Dry run: compress {summary.compressed}, copy {summary.copied}, "
            f"skip {summary.skipped}, conflicts {summary.conflict_skipped}."
        )
        return
    label = "Done" if summary.ok else "Finished with errors"
    print(
        f"{label}: compressed {summary.compressed}, copied {summary.copied}, "
        f"skipped {summary.skipped}, conflict-skipped "
        f"{summary.conflict_skipped}, deleted {summary.deleted}, "
        f"failed {summary.failed}."
    )
    if summary.source_bytes:
        saved_bytes = summary.source_bytes - summary.output_bytes
        ratio = (
            summary.source_bytes / summary.output_bytes
            if summary.output_bytes
            else float("inf")
        )
        time_ratio = (
            summary.encoding_seconds / summary.video_seconds
            if summary.video_seconds
            else 0.0
        )
        print(
            f"Data: {format_gb(summary.source_bytes)} -> "
            f"{format_gb(summary.output_bytes)} | saved {format_gb(saved_bytes)} "
            f"({ratio:.1f}x)."
        )
        print(
            f"Encoding: {format_duration(summary.encoding_seconds)} for "
            f"{format_duration(summary.video_seconds)} of video "
            f"({time_ratio:.2f}x video time)."
        )


def _run(args: argparse.Namespace) -> int:
    if args.command == "status":
        if args.gpu_device is not None and not args.gpu:
            raise ValueError("--gpu-device requires --gpu")
        compression_ratio = (
            args.ratio
            if args.ratio is not None
            else (GPU_COMPRESSION_RATIO if args.gpu else DEFAULT_COMPRESSION_RATIO)
        )
        encoding_time_ratio = (
            args.time_ratio
            if args.time_ratio is not None
            else (GPU_ENCODING_TIME_RATIO if args.gpu else DEFAULT_ENCODING_TIME_RATIO)
        )
        if args.gpu:
            print("Checking NVIDIA H.265 NVENC support ...", file=sys.stderr)
            gpu_settings = make_settings(gpu=True, gpu_device=args.gpu_device)
            inspect_nvenc(gpu_settings, require_environment=True)
        print(f"Scanning {args.input} ...", file=sys.stderr)
        print(status_report(args.input, compression_ratio, encoding_time_ratio))
        return 0
    if args.command == "inspect":
        print(f"Inspecting {args.input} ...", file=sys.stderr)
        _json_print(SeqReader(args.input).inspect(args.samples))
        return 0

    if args.command == "compress":
        settings = make_settings(
            args.codec,
            args.crf,
            args.preset,
            args.keyint,
            gpu=args.gpu,
            cq=args.cq,
            gpu_preset=args.gpu_preset,
            gpu_device=args.gpu_device,
        )
        if not args.quiet:
            label = "NVIDIA H.265 NVENC" if settings.is_gpu else settings.codec
            print(f"Checking the environment-local FFmpeg runtime ({label}) ...", file=sys.stderr)
        runtime = (
            inspect_nvenc(settings, require_environment=True)
            if settings.is_gpu
            else inspect_ffmpeg(
                require_environment=True,
                required_encoders=(settings.codec,),
            )
        )
        summary = compress_path(
            args.input,
            runtime,
            settings,
            dest=args.dest,
            delete=args.delete,
            dry_run=args.dry_run,
            force=args.force,
            yes=args.yes,
            quiet=args.quiet,
        )
        _print_compression_summary(summary, dry_run=args.dry_run)
        return 0 if summary.ok else 1
    raise AssertionError(args.command)


def main(argv: list[str] | None = None) -> int:
    try:
        return _run(_parser().parse_args(argv))
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"seqcomp: error: {exc}", file=sys.stderr)
        return 1
