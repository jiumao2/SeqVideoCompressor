# seqcomp — compression and backup for NorPix SEQ video

[![View SeqVideoCompressor on GitHub](https://img.shields.io/badge/GitHub-SeqVideoCompressor-blue.svg)](https://github.com/jiumao2/SeqVideoCompressor) [![PyPI version](https://img.shields.io/pypi/v/seqcomp.svg)](https://pypi.org/project/seqcomp/) [![Tests](https://github.com/jiumao2/SeqVideoCompressor/actions/workflows/tests.yml/badge.svg?branch=master&event=push)](https://github.com/jiumao2/SeqVideoCompressor/actions/workflows/tests.yml)

`seqcomp` is a command-line tool for validated compression and folder backup of JPEG-compressed NorPix `.seq` behavioral videos:

- **compress**: convert every valid `.seq + .seq.idx` pair to H.265, H.264, or AV1 MKV, an acquisition-timestamp NPY sidecar, and a validation manifest;
- **status**: report whether recordings in a folder are raw-only, compressed, or incomplete;
- **backup mode (`--dest`)**: mirror a whole folder into a destination; SEQ pairs are compressed, while every other file and empty directory is copied and verified;
- **single-file mode**: the same `compress` command also accepts one `.seq` file.

The CPU default remains H.265, CRF 18, `medium`, grayscale, closed GOP, a 250-frame keyframe interval, and no audio. Expected CPU performance is approximately 20× compression, with encoding time approximately 1.2× the video duration. CPU AV1 is an optional higher-compression mode using SVT-AV1, CRF 28, and preset 6; its approximate estimates are 38× compression and 0.93× video duration. `--gpu` selects NVIDIA H.265 NVENC with CQ 28 and preset `p5`; its default estimates are 10× compression and 0.25× video duration. Actual time and size depend on image content and hardware.

SEQ already stores every frame as a JPEG-compressed image. Decoding those JPEG frames and applying a lossless video codec cannot recover information discarded by JPEG and can produce files larger than the original SEQ. `seqcomp` therefore provides only H.264, H.265, and AV1 lossy compression, intended to remove additional temporal redundancy between adjacent behavioral-video frames.

Chinese version: [README_ZH.md](README_ZH.md).

## Installation

From PyPI:

```powershell
conda create -n seqcomp -c conda-forge python=3.11 ffmpeg x264 x265 svt-av1 -y
conda activate seqcomp
pip install seqcomp
```

From source (current development workflow):

```powershell
conda env create --file environment.yml
conda activate seqcomp
pip install -e . --no-deps
```

For an existing source environment, use `conda env update --name seqcomp --file environment.yml`. `seqcomp` only uses the FFmpeg installed inside the active environment. CPU mode checks the selected libx264, libx265, or libsvtav1 encoder; GPU mode additionally requires an NVENC-capable NVIDIA GPU and a compatible, current NVIDIA driver. No CUDA Toolkit Python package is required.

Add the environment's `Scripts` directory to the user PATH if `seqcomp` should be available in newly opened terminals without an explicit Python path.

## Quick start

```powershell
# Summarize all recordings, space savings, and estimated encoding time
seqcomp status <path-to-session>

# Compress all valid SEQ/IDX pairs recursively in place; sources are kept
seqcomp compress <path-to-session>

# Mirror backup: SEQ pairs are compressed, everything else copied and verified
seqcomp compress <path-to-session> --dest <backup-dir>

# Set codec parameters independently
seqcomp compress <path-to-session> --codec h265 --crf 18 --preset medium --keyint 250

# Use the optional CPU AV1 mode; this does not change the default codec
seqcomp compress <path-to-session> --codec av1 --crf 28 --av1-preset 6 --keyint 250

# Use the validated NVIDIA H.265 NVENC defaults
seqcomp compress <path-to-session> --gpu

# Adjust GPU quality, effort, and device independently
seqcomp compress <path-to-session> --gpu --cq 26 --gpu-preset p6 --gpu-device 0

# Estimate a GPU run and verify that NVENC is available
seqcomp status <path-to-session> --gpu

# Compress a single recording
seqcomp compress <path-to-recording.seq> --dest <output-dir>

# Delete each source pair immediately after its compressed package is verified
seqcomp compress <path-to-session> --dest <backup-dir> --delete

# Show the planned compression/copy/deletion actions without writing anything
seqcomp compress <path-to-session> --dest <backup-dir> --delete --dry-run
```

> **`--delete` is irreversible.** Each verified source pair is deleted immediately, so a later failure can leave the folder partially processed without restoring earlier sources. The default output is lossy and cannot recreate the original JPEG payloads or the original SEQ bytes. Keep a second independent copy until the compressed backup has also been checked in the intended downstream analysis.

## Command reference

**`seqcomp compress <file-or-folder> [--dest DIR] [--codec {h264,h265,av1}] [--crf N] [--preset NAME] [--av1-preset N] [--keyint N] [--gpu] [--cq N] [--gpu-preset {p1..p7}] [--gpu-device INDEX] [--delete] [--dry-run] [--force] [--yes] [--quiet]`**

- Recursively processes only `.seq` files with an exact `.seq.idx` sibling. Unpaired `.seq` files are ordinary files and are never interpreted as recordings.
- CPU defaults: `--codec h265 --crf 18 --preset medium --keyint 250`. AV1 is available only when explicitly selected. All codecs use IDX-guided JPEG payload extraction with FFmpeg MJPEG decoding; verification and SHA-256 calculation are always enabled.
- Long-running encoding, full-decode validation, SHA-256, large-file copy, and deletion-precheck stages display progress or an explicit activity message. `--quiet` hides progress bars and per-file messages but keeps the final summary.
- `--codec h265`: selects the video codec. `h265` is the default; `h264` may decode more broadly in older software; `av1` uses the CPU SVT-AV1 encoder for higher compression at the cost of more limited decoder compatibility.
- `--crf 18`: controls the CPU quality/size tradeoff. Lower values retain more image detail and produce larger files; higher values produce smaller files with more distortion. H.264/H.265 accept 1–51; AV1 accepts 1–63 and defaults to 28. CRF values are not directly comparable between codecs.
- `--preset medium`: controls libx264/libx265 CPU encoding effort. Slower presets spend more CPU time seeking compression efficiency; faster presets finish sooner. It cannot be combined with `--codec av1`.
- `--av1-preset 6`: controls SVT-AV1 CPU encoding effort from 0 (slowest) to 13 (fastest). It requires `--codec av1` and cannot be used with `--gpu`.
- `--gpu`: selects NVIDIA H.265 NVENC. It requires `--codec h265` and never silently falls back to CPU. Before scanning or hashing source files, seqcomp performs a real one-frame NVENC initialization test and reports FFmpeg, GPU, and driver diagnostics if it fails.
- `--cq 28`: controls the NVENC quality/size tradeoff from 1–51; lower is higher quality and larger. CQ and CPU CRF values are different rate-control scales and must not be compared numerically.
- `--gpu-preset p5`: selects NVENC effort from `p1` (fastest) through `p7` (slowest). `--gpu-device INDEX` selects a zero-based NVIDIA GPU; omission uses FFmpeg automatic selection.
- AV1 output is AV1 Main, 8-bit 4:2:0, full-range `bt470bg`, and contains no audio. Both even and odd source dimensions are preserved. GPU output uses the analogous HEVC Main format, but NVENC requires even width and height; odd-sized GPU inputs are rejected without padding or resizing and can still be compressed with a CPU codec.
- `--keyint 250`: sets the fixed keyframe interval and closed-GOP length to 250 frames, approximately 2.5 seconds at 100 fps. Shorter intervals improve seeking/random access but usually increase file size; longer intervals favor compression.
- **Without `--dest`**: writes `X.mkv + X.timestamps.npy + X.manifest.json` beside each source pair and leaves every unrelated file unchanged. Verified existing packages are skipped; unverified or conflicting outputs require confirmation, while `--force` replaces them.
- **With `--dest`**: mirror-backup mode — SEQ/IDX pairs become compressed packages in the corresponding destination folders; all other files are copied atomically and accepted only when their SHA-256 matches. The directory tree, including empty directories, is preserved.
- `--delete`: immediately after each package is compressed or matched to an existing manifest and fully verified, rehashes that source `.seq + .seq.idx` pair and deletes it. A failed pair is kept and the run is marked as failed, but later recordings and ordinary files are still attempted. Earlier verified deletions are not rolled back.
- `--dry-run`: reports the plan without creating, replacing, copying, or deleting files.
- `--yes`: accepts destination/conflict prompts for unattended runs. `--force` recompresses valid existing packages as well.
- Output names preserve the full source stem. For example, `20260726-20-12-06.000.seq` produces `20260726-20-12-06.000.mkv`, `20260726-20-12-06.000.timestamps.npy`, and `20260726-20-12-06.000.manifest.json`.
- `timestamps.npy` is a one-dimensional `int64` array. Each element stores the corresponding frame's absolute UNIX timestamp as an integer number of microseconds (μs).

**`seqcomp status <file-or-folder> [--gpu] [--gpu-device INDEX] [--ratio R] [--time-ratio R]`**

- Reports each discovered recording as `raw-only`, `both`, `compressed-only`, or `incomplete`, with sizes shown in decimal GB.
- The final folder summary combines all recordings and reports represented source size, actual/projected output size, total space saving, remaining video duration, and estimated remaining encoding time.
- CPU estimates default to `--ratio 20` and `--time-ratio 1.2`; `--gpu` uses 10 and 0.25 and first verifies NVENC support. For an AV1 planning estimate, use `--ratio 38 --time-ratio 0.93`. Explicit ratio options override either estimate without changing compression settings.
- Status is based on file existence and size; `compress` performs the full content validation.

**`seqcomp inspect <recording.seq> [--samples N]`**

- Validates the 1024-byte SEQ header, 24-byte IDX records, offsets, file-end alignment, JPEG boundaries, timestamps, and sampled IDX/SEQ timestamp agreement.

## Safety design

- **Automatic package validation**: every compression checks that sampled JPEG dimensions and the encoded-video dimensions match the SEQ header, that the output contains exactly one video stream and no audio, that its packet count equals the SEQ frame count, that the complete video decodes without FFmpeg errors, and that `timestamps.npy` is an `int64` array exactly equal to all IDX acquisition timestamps. Native-gray H.264/H.265 and Main/4:2:0 full-range AV1/NVENC metadata are checked separately.
- **SHA-256 is mandatory**: source SEQ/IDX files, output MKV/timestamps, and all mirrored ordinary files are always hashed. There is no option to disable hashing.
- **Atomic file writes**: video, timestamps, JSON, and mirrored files are written through tool-specific temporary names and renamed into place. Because one recording consists of three outputs, interruption between renames can leave an incomplete package; the next run detects it and refuses to treat it as verified.
- **Per-recording verified deletion**: only explicit `--delete` can remove source pairs. Each SEQ/IDX pair is rehashed against its manifest immediately after its compressed package passes validation, then deleted before the next recording starts. A changed or failed pair is kept, but it does not roll back earlier verified deletions.
- **Verified folder backup**: `.seqcomp_manifest.json` records source/output hashes and codec parameters. Re-runs skip unchanged verified content and detect modified sources, tampered outputs, and same-name/different-content ordinary files.
- **Source/destination isolation**: source and destination folders may not contain one another. Unrelated temporary-looking user files are never cleaned up.
- **Space check before writing**: before a real run, the destination is checked against a conservative 2x compression floor plus package and mirrored-file overhead. Insufficient space stops the run before any output or deletion.
- **No byte-exact restore**: H.264, H.265, and AV1 output is lossy, so this project does not provide a command that claims to restore the original `.seq` file. Keep `X.mkv + X.timestamps.npy + X.manifest.json` together.

## License

This project is licensed under the [GNU General Public License v3.0](LICENSE).
