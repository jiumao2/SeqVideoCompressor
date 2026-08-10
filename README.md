# seqcomp — compression and backup for NorPix SEQ video

[![View SeqVideoCompressor on GitHub](https://img.shields.io/badge/GitHub-SeqVideoCompressor-blue.svg)](https://github.com/jiumao2/SeqVideoCompressor) [![PyPI version](https://img.shields.io/pypi/v/seqcomp.svg)](https://pypi.org/project/seqcomp/) [![Tests](https://github.com/jiumao2/SeqVideoCompressor/actions/workflows/tests.yml/badge.svg?branch=master&event=push)](https://github.com/jiumao2/SeqVideoCompressor/actions/workflows/tests.yml)

`seqcomp` is a command-line tool for validated compression and folder backup of JPEG-compressed NorPix `.seq` behavioral videos:

- **compress**: convert every valid `.seq + .seq.idx` pair to H.265/H.264 MKV, an acquisition-timestamp NPY sidecar, and a validation manifest;
- **status**: report whether recordings in a folder are raw-only, compressed, or incomplete;
- **backup mode (`--dest`)**: mirror a whole folder into a destination; SEQ pairs are compressed, while every other file and empty directory is copied and verified;
- **single-file mode**: the same `compress` command also accepts one `.seq` file.

The default is H.265, CRF 18, `medium`, grayscale, closed GOP, a 250-frame keyframe interval, and no audio. Expected performance is approximately 20× compression, with encoding time approximately 1.2× the video duration. Actual time and size depend on image content and hardware.

SEQ already stores every frame as a JPEG-compressed image. Decoding those JPEG frames and applying a lossless video codec cannot recover information discarded by JPEG and, on the tested data, produces files larger than the original SEQ. `seqcomp` therefore provides only H.264/H.265 CRF lossy compression, intended to remove additional temporal redundancy between adjacent behavioral-video frames.

Chinese version: [README_ZH.md](README_ZH.md).

## Installation

From PyPI:

```powershell
conda create -n seqcomp -c conda-forge python=3.11 ffmpeg x264 x265 -y
conda activate seqcomp
pip install seqcomp
```

From source (current development workflow):

```powershell
conda env create --file environment.yml
conda activate seqcomp
pip install -e . --no-deps
```

For an existing source environment, use `conda env update --name seqcomp --file environment.yml`. `seqcomp` only uses the FFmpeg installed inside the active environment and checks that libx264, libx265, and grayscale input are available.

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

# Compress a single recording
seqcomp compress <path-to-recording.seq> --dest <output-dir>

# Delete source .seq and .seq.idx only after the backup has passed validation
seqcomp compress <path-to-session> --dest <backup-dir> --delete

# Show the planned compression/copy/deletion actions without writing anything
seqcomp compress <path-to-session> --dest <backup-dir> --delete --dry-run
```

> **`--delete` is irreversible.** The default output is lossy and cannot recreate the original JPEG payloads or the original SEQ bytes. Keep a second independent copy until the compressed backup has also been checked in the intended downstream analysis.

## Command reference

**`seqcomp compress <file-or-folder> [--dest DIR] [--codec {h264,h265}] [--crf N] [--preset NAME] [--keyint N] [--pipeline NAME] [--delete] [--dry-run] [--force] [--yes] [--quiet]`**

- Recursively processes only `.seq` files with an exact `.seq.idx` sibling. Unpaired `.seq` files are ordinary files and are never interpreted as recordings.
- Defaults: `--codec h265 --crf 18 --preset medium --keyint 250 --pipeline jpeg-pipe`; verification and SHA-256 calculation are always enabled.
- Long-running encoding, full-decode validation, SHA-256, large-file copy, and deletion-precheck stages display progress or an explicit activity message. `--quiet` hides progress bars and per-file messages but keeps the final summary.
- `--codec h265`: selects the video codec. `h265` usually provides better compression efficiency for long-term storage; `h264` may decode more broadly in older software.
- `--crf 18`: controls the quality/size tradeoff. Lower values retain more image detail and produce larger files; higher values produce smaller files with more distortion. The accepted range is 1–51, and CRF values should be compared within the same codec.
- `--preset medium`: controls encoding effort. Slower presets spend more CPU time seeking compression efficiency; faster presets finish sooner. The default `medium` balances archival compression and encoding time.
- `--keyint 250`: sets the fixed keyframe interval and closed-GOP length to 250 frames, approximately 2.5 seconds at 100 fps. Shorter intervals improve seeking/random access but usually increase file size; longer intervals favor compression.
- **Without `--dest`**: writes `X.mkv + X.timestamps.npy + X.manifest.json` beside each source pair and leaves every unrelated file unchanged. Verified existing packages are skipped; unverified or conflicting outputs require confirmation, while `--force` replaces them.
- **With `--dest`**: mirror-backup mode — SEQ/IDX pairs become compressed packages in the corresponding destination folders; all other files are copied atomically and accepted only when their SHA-256 matches. The directory tree, including empty directories, is preserved.
- `--delete`: after the complete run succeeds, removes the source `.seq` and `.seq.idx` only for packages verified in this run or matched to an existing manifest by source/output SHA-256 and structural checks. Any failure or unresolved conflict keeps all pending source pairs.
- `--dry-run`: reports the plan without creating, replacing, copying, or deleting files.
- `--yes`: accepts destination/conflict prompts for unattended runs. `--force` recompresses valid existing packages as well.
- Output names preserve the full source stem. For example, `20260726-20-12-06.000.seq` produces `20260726-20-12-06.000.mkv`, `20260726-20-12-06.000.timestamps.npy`, and `20260726-20-12-06.000.manifest.json`.
- `timestamps.npy` is a one-dimensional `int64` array. Each element stores the corresponding frame's absolute UNIX timestamp as an integer number of microseconds (μs).

**`seqcomp status <file-or-folder> [--ratio R] [--time-ratio R]`**

- Reports each discovered recording as `raw-only`, `both`, `compressed-only`, or `incomplete`, with sizes shown in decimal GB.
- The final folder summary combines all recordings and reports represented source size, actual/projected output size, total space saving, remaining video duration, and estimated remaining encoding time.
- Raw-only estimates default to `--ratio 20` and `--time-ratio 1.2`. These assumptions can be changed without affecting compression settings.
- Status is based on file existence and size; `compress` performs the full content validation.

**`seqcomp inspect <recording.seq> [--samples N]`**

- Validates the 1024-byte SEQ header, 24-byte IDX records, offsets, file-end alignment, JPEG boundaries, timestamps, and sampled IDX/SEQ timestamp agreement.

## Safety design

- **Automatic package validation**: every compression checks that sampled JPEG dimensions and the encoded-video dimensions match the SEQ header, that the output contains exactly one video stream and no audio, that its packet count equals the SEQ frame count, that the complete video decodes without FFmpeg errors, and that `timestamps.npy` is an `int64` array exactly equal to all IDX acquisition timestamps. Native-gray HEVC output is also checked.
- **SHA-256 is mandatory**: source SEQ/IDX files, output MKV/timestamps, and all mirrored ordinary files are always hashed. There is no option to disable hashing.
- **Atomic file writes**: video, timestamps, JSON, and mirrored files are written through tool-specific temporary names and renamed into place. Because one recording consists of three outputs, interruption between renames can leave an incomplete package; the next run detects it and refuses to treat it as verified.
- **Conservative deletion**: only explicit `--delete` can remove source pairs. Deletion waits until the complete run has no failed copy, failed compression, or unresolved conflict. Immediately before deleting anything, every source SEQ/IDX pair is rehashed and compared with its manifest; if any source changed, all pending source pairs are kept.
- **Verified folder backup**: `.seqcomp_manifest.json` records source/output hashes and codec parameters. Re-runs skip unchanged verified content and detect modified sources, tampered outputs, and same-name/different-content ordinary files.
- **Source/destination isolation**: source and destination folders may not contain one another. Unrelated temporary-looking user files are never cleaned up.
- **Space check before writing**: before a real run, the destination is checked against a conservative 2x compression floor plus package and mirrored-file overhead. Insufficient space stops the run before any output or deletion.
- **No byte-exact restore**: H.264/H.265 CRF output is lossy, so this project does not provide a command that claims to restore the original `.seq` file. Keep `X.mkv + X.timestamps.npy + X.manifest.json` together.

## License

This project is licensed under the [GNU General Public License v3.0](LICENSE).
