# seqcomp —— NorPix SEQ 视频压缩与备份工具

[![在 GitHub 查看 SeqVideoCompressor](https://img.shields.io/badge/GitHub-SeqVideoCompressor-blue.svg)](https://github.com/jiumao2/SeqVideoCompressor) [![PyPI 版本](https://img.shields.io/pypi/v/seqcomp.svg)](https://pypi.org/project/seqcomp/) [![测试](https://github.com/jiumao2/SeqVideoCompressor/actions/workflows/tests.yml/badge.svg?branch=master&event=push)](https://github.com/jiumao2/SeqVideoCompressor/actions/workflows/tests.yml)

`seqcomp` 是一个用于 JPEG 压缩 NorPix `.seq` 行为视频的命令行工具，提供经过校验的压缩和文件夹备份：

- **compress**：把每个有效的 `.seq + .seq.idx` 文件对转换为 H.265/H.264 MKV、采集时间戳 NPY sidecar 和校验 manifest；
- **status**：报告文件夹中的视频是仅有原始文件、已压缩，还是输出不完整；
- **备份模式（`--dest`）**：把整个文件夹镜像到目标目录，SEQ 文件对压缩存储，其余文件和空目录原样复制并校验；
- **单文件模式**：同一个 `compress` 命令也可以接收一个 `.seq` 文件。

默认参数为 H.265、CRF 18、`medium`、灰度、closed GOP、250 帧关键帧间隔且无音频。预计压缩率约为 20×，编码时间约为视频时长的 1.2×。实际时间和体积会随画面内容和计算机性能变化。

SEQ 已经把每一帧保存为经过 JPEG 压缩的图像。把这些 JPEG 帧解码后再使用无损视频编码，无法恢复 JPEG 已丢弃的信息；在现有测试数据上，生成的文件反而大于原始 SEQ。因此，`seqcomp` 只提供 H.264/H.265 CRF 有损压缩，用于进一步去除行为视频相邻帧之间的时间冗余。

英文版：[README.md](README.md)。

## 安装

从 PyPI 安装：

```powershell
conda create -n seqcomp -c conda-forge python=3.11 ffmpeg x264 x265 -y
conda activate seqcomp
pip install seqcomp
```

从源码安装（当前开发方式）：

```powershell
conda env create --file environment.yml
conda activate seqcomp
pip install -e . --no-deps
```

如果已有源码环境，可执行 `conda env update --name seqcomp --file environment.yml`。`seqcomp` 只使用当前环境内安装的 FFmpeg，并检查 libx264、libx265 和灰度输入支持。

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

# 压缩单个视频
seqcomp compress <视频路径.seq> --dest <输出目录>

# 只有备份通过校验后才删除源 .seq 和 .seq.idx
seqcomp compress <会话路径> --dest <备份目录> --delete

# 只显示计划中的压缩、复制和删除操作，不写入文件
seqcomp compress <会话路径> --dest <备份目录> --delete --dry-run
```

> **`--delete` 不可逆。** 默认输出是有损视频，无法重建原始 JPEG payload 或原始 SEQ 字节。请至少保留一份独立副本，直到压缩备份也通过实际下游分析检查。

## 命令参考

**`seqcomp compress <文件或文件夹> [--dest DIR] [--codec {h264,h265}] [--crf N] [--preset NAME] [--keyint N] [--pipeline NAME] [--delete] [--dry-run] [--force] [--yes] [--quiet]`**

- 递归处理仅限具有同名 `.seq.idx` sibling 的 `.seq` 文件。未配对的 `.seq` 只作为普通文件处理，绝不会被解释为视频。
- 默认值：`--codec h265 --crf 18 --preset medium --keyint 250 --pipeline jpeg-pipe`；校验和 SHA-256 计算始终启用。
- 编码、完整解码校验、SHA-256、大文件复制和删除前复核等耗时阶段都会显示进度条或明确的运行提示。`--quiet` 会隐藏进度条和逐文件消息，但仍保留最终汇总。
- `--codec h265`：选择视频编码格式。`h265` 通常具有更高的压缩效率，适合长期存储；`h264` 在较旧软件中的解码兼容性可能更好。
- `--crf 18`：控制画质与文件体积的权衡。数值越低，保留的图像细节越多、文件越大；数值越高，文件越小、失真越明显。允许范围为 1–51，CRF 数值应在同一种 codec 内比较。
- `--preset medium`：控制编码投入。较慢的 preset 使用更多 CPU 时间寻找更高的压缩效率，较快的 preset 更快完成；默认 `medium` 在归档压缩与编码时间之间折中。
- `--keyint 250`：把固定关键帧间隔和 closed-GOP 长度设为 250 帧，在 100 fps 下约为 2.5 秒。较短间隔有利于 seek/随机访问，但通常增加文件体积；较长间隔更有利于压缩。
- **无 `--dest`**：在每个源文件对旁生成 `X.mkv + X.timestamps.npy + X.manifest.json`，所有无关文件保持不变。已经验证的输出会跳过；未验证或冲突的输出需要确认，`--force` 可替换它们。
- **有 `--dest`**：镜像备份模式 —— SEQ/IDX 文件对在目标目录对应位置转换为压缩包，其余文件原子复制，并且只有 SHA-256 一致才视为成功；完整目录结构和空目录都会保留。
- `--delete`：整次运行成功后，只删除本次验证成功，或通过源文件/输出 SHA-256 与现有 manifest 及结构检查确认的源 `.seq` 和 `.seq.idx`。任何失败或未解决冲突都会保留所有等待删除的源文件对。
- `--dry-run`：只报告计划，不创建、替换、复制或删除文件。
- `--yes`：在无人值守运行中接受目标目录和冲突确认。`--force` 还会重新压缩已经验证的输出。
- 输出文件名完整保留源文件 stem。例如，`20260726-20-12-06.000.seq` 生成 `20260726-20-12-06.000.mkv`、`20260726-20-12-06.000.timestamps.npy` 和 `20260726-20-12-06.000.manifest.json`。
- `timestamps.npy` 是一维 `int64` 数组。每个元素以微秒（μs）为单位，用整数存储对应视频帧的绝对 UNIX 时间戳。

**`seqcomp status <文件或文件夹> [--ratio R] [--time-ratio R]`**

- 把每个发现的视频报告为 `raw-only`、`both`、`compressed-only` 或 `incomplete`，体积统一以十进制 GB 显示。
- 文件夹末尾会汇总全部视频的源文件体积、实际或预计输出体积、总节省空间、尚未压缩的视频时长和预计剩余编码时间。
- 对 `raw-only` 视频默认按 `--ratio 20` 和 `--time-ratio 1.2` 估算；修改这些假设不会改变实际压缩参数。
- `status` 只依据文件是否存在和体积判断；完整内容校验由 `compress` 执行。

**`seqcomp inspect <视频.seq> [--samples N]`**

- 校验 1024-byte SEQ header、24-byte IDX record、offset、文件末尾对齐、JPEG 边界、时间戳以及抽样 IDX/SEQ 时间戳一致性。

## 安全设计

- **自动压缩包校验**：每次压缩都检查抽样 JPEG 尺寸和编码视频尺寸与 SEQ header 一致、输出恰好只有一个视频 stream 且没有音频、video packet 数与 SEQ 帧数相等、完整视频可以由 FFmpeg 无错误解码，并确认 `timestamps.npy` 是与全部 IDX 采集时间戳逐项相等的 `int64` 数组；同时检查 HEVC 原生灰度输出。
- **强制 SHA-256**：源 SEQ/IDX、输出 MKV/timestamps 和镜像复制的所有普通文件始终计算哈希，不提供关闭哈希的参数。
- **原子文件写入**：视频、时间戳、JSON 和镜像文件先写入工具专用临时名称，再改名为正式文件。一个视频包含三个输出，因此在多次改名之间中断可能留下不完整压缩包；下次运行会检测到，并拒绝把它当作已验证输出。
- **保守删除**：只有显式 `--delete` 才能删除源文件对。删除会等待整次运行结束，并要求不存在复制失败、压缩失败或未解决冲突。删除任何文件前会立即重新计算每一对源 SEQ/IDX 的哈希并与 manifest 比较；只要任一源文件发生变化，所有等待删除的源文件对都会保留。
- **经过校验的文件夹备份**：`.seqcomp_manifest.json` 记录源文件/输出哈希和编码参数。重复运行会跳过未改变且通过验证的内容，并识别源文件变化、输出篡改以及同名但内容不同的普通文件。
- **源目录与目标目录隔离**：源目录和目标目录不能互相包含。工具不会清理名称类似临时文件的无关用户文件。
- **写入前空间检查**：实际运行前按照保守的 2 倍压缩率下限，加上压缩包和镜像文件开销检查目标空间；空间不足会在生成任何输出或删除源文件前停止。
- **不提供原字节恢复**：H.264/H.265 CRF 输出是有损的，因此本项目不会提供声称能够恢复原始 `.seq` 的命令。请始终保留 `X.mkv + X.timestamps.npy + X.manifest.json` 三件套。

## License

本项目使用 [GNU General Public License v3.0](LICENSE) 许可。
