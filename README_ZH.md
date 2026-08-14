# seqcomp —— NorPix SEQ 视频压缩与备份工具

[![在 GitHub 查看 SeqVideoCompressor](https://img.shields.io/badge/GitHub-SeqVideoCompressor-blue.svg)](https://github.com/jiumao2/SeqVideoCompressor) [![PyPI 版本](https://img.shields.io/pypi/v/seqcomp.svg)](https://pypi.org/project/seqcomp/) [![测试](https://github.com/jiumao2/SeqVideoCompressor/actions/workflows/tests.yml/badge.svg?branch=master&event=push)](https://github.com/jiumao2/SeqVideoCompressor/actions/workflows/tests.yml)

`seqcomp` 是一个用于 JPEG 压缩 NorPix `.seq` 行为视频的命令行工具，提供经过校验的压缩和文件夹备份：

- **compress**：把每个有效的 `.seq + .seq.idx` 文件对转换为 H.265、H.264 或 AV1 MKV、采集时间戳 NPY sidecar 和校验 manifest；
- **status**：报告文件夹中的视频是仅有原始文件、已压缩，还是输出不完整；
- **备份模式（`--dest`）**：把整个文件夹镜像到目标目录，SEQ 文件对压缩存储，其余文件和空目录原样复制并校验；
- **单文件模式**：同一个 `compress` 命令也可以接收一个 `.seq` 文件。

CPU 默认参数仍为 H.265、CRF 18、`medium`、灰度、closed GOP、250 帧关键帧间隔且无音频。CPU 预计压缩率约为 20×，编码时间约为视频时长的 1.2×。CPU AV1 是可选的高压缩率方案，使用 SVT-AV1、CRF 28 和 preset 6，预估压缩率约为 38×，编码时间约为视频时长的 0.93×。`--gpu` 使用 NVIDIA H.265 NVENC、CQ 28 和 `p5`，默认估算为 10× 压缩率和 0.25× 视频时长。实际时间和体积会随画面内容和计算机性能变化。

SEQ 已经把每一帧保存为经过 JPEG 压缩的图像。把这些 JPEG 帧解码后再使用无损视频编码，无法恢复 JPEG 已丢弃的信息，而且可能生成比原始 SEQ 更大的文件。因此，`seqcomp` 只提供 H.264、H.265 和 AV1 有损压缩，用于进一步去除行为视频相邻帧之间的时间冗余。

英文版：[README.md](README.md)。

## 安装

从 PyPI 安装：

```powershell
conda create -n seqcomp -c conda-forge python=3.11 ffmpeg x264 x265 svt-av1 -y
conda activate seqcomp
pip install seqcomp
```

从源码安装（当前开发方式）：

```powershell
conda env create --file environment.yml
conda activate seqcomp
pip install -e . --no-deps
```

如果已有源码环境，可执行 `conda env update --name seqcomp --file environment.yml`。`seqcomp` 只使用当前环境内安装的 FFmpeg。CPU 模式检查所选 libx264、libx265 或 libsvtav1 编码器；GPU 模式还需要支持 NVENC 的 NVIDIA GPU 和兼容的最新 NVIDIA 驱动，不需要额外安装 CUDA Toolkit Python package。

如果希望在新终端中不指定 Python 路径即可运行 `seqcomp`，请把该环境的 `Scripts` 目录加入用户 PATH。

## 快速上手

```powershell
# 汇总所有视频、节省空间和预计编码时间
seqcomp status <会话路径>

# 递归就地压缩所有有效 SEQ/IDX 文件对；保留源文件
seqcomp compress <会话路径>

# 镜像备份：SEQ 文件对压缩，其余文件复制并校验
seqcomp compress <会话路径> --dest <备份目录>

# 分别设置各项编码参数
seqcomp compress <会话路径> --codec h265 --crf 18 --preset medium --keyint 250

# 使用可选的 CPU AV1 方案；默认 codec 不会因此改变
seqcomp compress <会话路径> --codec av1 --crf 28 --av1-preset 6 --keyint 250

# 使用经过验证的 NVIDIA H.265 NVENC 默认参数
seqcomp compress <会话路径> --gpu

# 分别调整 GPU 画质、编码投入和设备
seqcomp compress <会话路径> --gpu --cq 26 --gpu-preset p6 --gpu-device 0

# 估算 GPU 压缩并确认 NVENC 可用
seqcomp status <会话路径> --gpu

# 压缩单个视频
seqcomp compress <视频路径.seq> --dest <输出目录>

# 每个压缩包通过校验后立即删除对应的源文件对
seqcomp compress <会话路径> --dest <备份目录> --delete

# 只显示计划中的压缩、复制和删除操作，不写入文件
seqcomp compress <会话路径> --dest <备份目录> --delete --dry-run

# 校验并删除编码中断后遗留的过期临时视频
seqcomp compress <会话路径> --cleanup-temp

# 只预览临时文件清理，不删除任何内容
seqcomp compress <会话路径> --cleanup-temp --dry-run
```

> **`--delete` 不可逆。** 每个通过校验的源文件对都会立即删除，因此后续文件失败时，文件夹可能处于部分完成状态，先前删除的源文件不会恢复。默认输出是有损视频，无法重建原始 JPEG payload 或原始 SEQ 字节。请至少保留一份独立副本，直到压缩备份也通过实际下游分析检查。

## 命令参考

**`seqcomp compress <文件或文件夹> [--dest DIR] [--codec {h264,h265,av1}] [--crf N] [--preset NAME] [--av1-preset N] [--keyint N] [--gpu] [--cq N] [--gpu-preset {p1..p7}] [--gpu-device INDEX] [--delete] [--cleanup-temp] [--dry-run] [--force] [--yes] [--quiet]`**

- 递归处理仅限具有同名 `.seq.idx` sibling 的 `.seq` 文件。未配对的 `.seq` 只作为普通文件处理，绝不会被解释为视频。
- CPU 默认值：`--codec h265 --crf 18 --preset medium --keyint 250`。AV1 仅在显式选择时启用。所有 codec 均使用 IDX 引导的 JPEG payload 提取和 FFmpeg MJPEG 解码；校验和 SHA-256 计算始终启用。
- 编码、完整解码校验、SHA-256、大文件复制和删除前复核等耗时阶段都会显示进度条或明确的运行提示。`--quiet` 会隐藏进度条和逐文件消息，但仍保留最终汇总。
- `--codec h265`：选择视频编码格式。`h265` 是默认值；`h264` 在较旧软件中的解码兼容性可能更好；`av1` 使用 CPU SVT-AV1 编码器，以较有限的解码兼容性换取更高压缩率。
- `--crf 18`：控制 CPU 画质与文件体积的权衡。数值越低，保留的图像细节越多、文件越大；数值越高，文件越小、失真越明显。H.264/H.265 允许 1–51；AV1 允许 1–63，且默认值为 28。不同 codec 的 CRF 数值不能直接比较。
- `--preset medium`：控制 libx264/libx265 的 CPU 编码投入。较慢的 preset 使用更多 CPU 时间寻找更高压缩效率，较快的 preset 更快完成；不能与 `--codec av1` 同时使用。
- `--av1-preset 6`：控制 SVT-AV1 的 CPU 编码投入，范围为 0（最慢）到 13（最快）。它要求 `--codec av1`，且不能与 `--gpu` 同时使用。
- `--gpu`：使用 NVIDIA H.265 NVENC，仅支持 `--codec h265`，不会静默回退 CPU。在扫描或哈希源文件之前，seqcomp 会实际执行一帧 NVENC 初始化测试；失败时报告 FFmpeg、GPU 和驱动诊断。
- `--cq 28`：控制 NVENC 画质与体积，范围为 1–51，越低画质越高、文件越大。CQ 与 CPU CRF 是不同的码率控制尺度，不能直接比较数值。
- `--gpu-preset p5`：选择从 `p1`（最快）到 `p7`（最慢）的 NVENC 编码投入。`--gpu-device INDEX` 选择从零开始编号的 NVIDIA GPU；省略时由 FFmpeg 自动选择。
- AV1 输出为 AV1 Main、8-bit 4:2:0、full-range `bt470bg` 且没有音频，偶数和奇数源尺寸都会原样保留。GPU 输出使用对应的 HEVC Main 格式，但 NVENC 要求宽高均为偶数；奇数尺寸 GPU 输入会在不补边、不缩放的前提下被拒绝，仍可使用 CPU codec 压缩。
- `--keyint 250`：把固定关键帧间隔和 closed-GOP 长度设为 250 帧，在 100 fps 下约为 2.5 秒。较短间隔有利于 seek/随机访问，但通常增加文件体积；较长间隔更有利于压缩。
- **无 `--dest`**：在每个源文件对旁生成 `X.mkv + X.timestamps.npy + X.manifest.json`，所有无关文件保持不变。已经验证的输出会跳过；未验证或冲突的输出需要确认，`--force` 可替换它们。
- **有 `--dest`**：镜像备份模式 —— SEQ/IDX 文件对在目标目录对应位置转换为压缩包，其余文件原子复制，并且只有 SHA-256 一致才视为成功；完整目录结构和空目录都会保留。
- `--delete`：每个压缩包完成压缩，或与现有 manifest 匹配并通过完整校验后，立即重新哈希对应的源 `.seq + .seq.idx` 并删除。某个文件对失败时会保留该文件对，并将本次运行标记为失败；程序仍会继续尝试后续视频和普通文件，此前已经验证并删除的源文件不会恢复。
- `--cleanup-temp`：扫描输出目录树中因编码中断遗留的 seqcomp 视频临时文件。只有同时满足以下条件才会删除：文件名严格符合 seqcomp 的专用临时格式、文件至少已有 24 小时、对应的 `X.mkv + X.timestamps.npy + X.manifest.json` 正式压缩包通过 SHA-256、stream、尺寸、packet 数、时间戳和完整解码校验，并且临时文件在校验期间未发生变化。如果源 SEQ/IDX 文件对仍存在，两者的哈希还必须与 manifest 一致。较新、有歧义、发生变化、被占用或无法验证的文件都会保留。即使已经没有源文件对，也可以单独执行清理。
- `--dry-run`：只报告计划，不创建、替换、复制或删除文件。
- `--yes`：在无人值守运行中接受目标目录和冲突确认。`--force` 还会重新压缩已经验证的输出。
- 输出文件名完整保留源文件 stem。例如，`20260726-20-12-06.000.seq` 生成 `20260726-20-12-06.000.mkv`、`20260726-20-12-06.000.timestamps.npy` 和 `20260726-20-12-06.000.manifest.json`。
- `timestamps.npy` 是一维 `int64` 数组。每个元素以微秒（μs）为单位，用整数存储对应视频帧的绝对 UNIX 时间戳。

**`seqcomp status <文件或文件夹> [--gpu] [--gpu-device INDEX] [--ratio R] [--time-ratio R]`**

- 把每个发现的视频报告为 `raw-only`、`both`、`compressed-only` 或 `incomplete`，体积统一以十进制 GB 显示。
- 严格符合 seqcomp 视频临时命名的文件不计入 recording 数量，而是在单独的 `Temporary files` 小节中报告；`status` 绝不会删除它们。
- 文件夹末尾会汇总全部视频的源文件体积、实际或预计输出体积、总节省空间、尚未压缩的视频时长和预计剩余编码时间。
- CPU 默认按 `--ratio 20` 和 `--time-ratio 1.2` 估算；`--gpu` 使用 10 和 0.25，并先确认 NVENC 可用。规划 AV1 时可使用 `--ratio 38 --time-ratio 0.93`。显式提供 ratio 参数可分别覆盖估算，但不会改变实际压缩参数。
- `status` 只依据文件是否存在和体积判断；完整内容校验由 `compress` 执行。

**`seqcomp inspect <视频.seq> [--samples N]`**

- 校验 1024-byte SEQ header、24-byte IDX record、offset、文件末尾对齐、JPEG 边界、时间戳以及抽样 IDX/SEQ 时间戳一致性。

## 安全设计

- **自动压缩包校验**：每次压缩都检查抽样 JPEG 尺寸和编码视频尺寸与 SEQ header 一致、输出恰好只有一个视频 stream 且没有音频、video packet 数与 SEQ 帧数相等、完整视频可以由 FFmpeg 无错误解码，并确认 `timestamps.npy` 是与全部 IDX 采集时间戳逐项相等的 `int64` 数组；原生灰度 H.264/H.265 与 Main/4:2:0 full-range AV1/NVENC 元数据会分别校验。
- **强制 SHA-256**：源 SEQ/IDX、输出 MKV/timestamps 和镜像复制的所有普通文件始终计算哈希，不提供关闭哈希的参数。
- **原子文件写入与显式清理**：视频、时间戳、JSON 和镜像文件先写入工具专用临时名称，再改名为正式文件。进程或系统突然中断可能遗留部分写入的视频临时文件。它们不会计入 recording 数量，也不会被自动删除；可使用 `--cleanup-temp`（建议先配合 `--dry-run`）执行上述年龄与完整校验后再清理。
- **逐视频验证后删除**：只有显式 `--delete` 才能删除源文件对。每个 SEQ/IDX 文件对的压缩包通过校验后，会立即重新计算源文件哈希并与 manifest 比较，随后在处理下一个视频前删除。发生变化或失败的文件对会保留，但不会回滚此前已经验证的删除。
- **经过校验的文件夹备份**：`.seqcomp_manifest.json` 记录源文件/输出哈希和编码参数。重复运行会跳过未改变且通过验证的内容，并识别源文件变化、输出篡改以及同名但内容不同的普通文件。
- **源目录与目标目录隔离**：源目录和目标目录不能互相包含。任何未满足全部清理规则的疑似临时文件都会保留。
- **写入前空间检查**：实际运行前按照保守的 2 倍压缩率下限，加上压缩包和镜像文件开销检查目标空间；空间不足会在生成任何输出或删除源文件前停止。
- **不提供原字节恢复**：H.264、H.265 和 AV1 输出均为有损视频，因此本项目不会提供声称能够恢复原始 `.seq` 的命令。请始终保留 `X.mkv + X.timestamps.npy + X.manifest.json` 三件套。

## License

本项目使用 [GNU General Public License v3.0](LICENSE) 许可。
