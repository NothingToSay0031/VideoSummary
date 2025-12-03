"""
本文件用于去除 yt-dlp 下载字幕的重复部分。

功能说明：
- 解析 SRT 字幕文件，识别并去除相邻字幕块之间的重复内容
- 通过检测文本重叠来合并重复的字幕条目
- 处理 input 目录下的所有 .srt 文件，生成去重后的字幕文件

使用方法：
- 将需要处理的 .srt 文件放入 input 目录
- 运行脚本，会自动处理所有字幕文件并生成去重后的版本
"""

import glob
import os


def is_timecode(line):
    """判断一行是否是时间轴"""
    return "-->" in line and line[0].isdigit()


def parse_srt_robust(file_path):
    """
    稳健的 SRT 解析器：不依赖空行，而是根据时间轴特征来切分。
    解决 '序号和时间轴被当成文本' 的 Bug。
    """
    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    blocks = []
    current_block = {"seq": None, "time": None, "text_lines": []}

    i = 0
    while i < len(lines):
        line = lines[i].strip()

        # 核心逻辑：扫描到时间轴，说明抓到了一个新块的“骨架”
        if is_timecode(line):
            # 1. 保存上一个块（如果存在）
            if current_block["time"]:
                blocks.append(current_block)

            # 2. 开始新块
            # 时间轴的前一行通常是序号，尝试获取
            seq = "0"
            if i > 0 and lines[i-1].strip().isdigit():
                seq = lines[i-1].strip()

            current_block = {
                "seq": seq,
                "time": line,
                "text_lines": []  # 准备接收接下来的文本
            }

        # 如果不是时间轴，也不是时间轴前面的那个序号，那就是文本内容
        elif line and not line.isdigit():
            # 防止把下一行的序号误读为文本：
            # 只有当下一行不是时间轴时，当前行才可能是文本
            is_next_line_time = (
                i + 1 < len(lines) and is_timecode(lines[i+1].strip()))
            if not is_next_line_time:
                if current_block["time"]:  # 确保已经在一个块里了
                    current_block["text_lines"].append(line)

        i += 1

    # 别忘了保存最后一个块
    if current_block["time"]:
        blocks.append(current_block)

    return blocks


def get_longest_overlap(s1, s2):
    """计算重叠长度逻辑 (保持不变)"""
    if not s1 or not s2:
        return 0
    min_overlap = 4
    max_possible = min(len(s1), len(s2))
    for length in range(max_possible, min_overlap - 1, -1):
        if s1.endswith(s2[:length]):
            return length
    return 0


def process_srt(file_path):
    print(f"正在处理: {file_path}")
    blocks = parse_srt_robust(file_path)

    if not blocks:
        print("  -> 空文件或解析失败")
        return

    final_blocks = []
    prev_text = ""

    for block in blocks:
        # 将多行文本合并为一行，去空格
        current_text_raw = " ".join(block["text_lines"]).strip()

        # 去重逻辑
        if not final_blocks:
            final_blocks.append((block["time"], current_text_raw))
            prev_text = current_text_raw
            continue

        overlap_len = get_longest_overlap(prev_text, current_text_raw)

        if overlap_len > 0:
            new_text = current_text_raw[overlap_len:].strip()
        else:
            new_text = current_text_raw

        # 过滤掉空的或者只有标点的行
        if new_text and len(new_text) > 1:
            final_blocks.append((block["time"], new_text))
            prev_text = new_text

    # 写入新文件
    new_filename = file_path.replace(".srt", "_fixed.srt")
    with open(new_filename, 'w', encoding='utf-8') as f:
        for index, (time, text) in enumerate(final_blocks, 1):
            f.write(f"{index}\n{time}\n{text}\n\n")

    print(f"  -> 完成! 原始行数: {len(blocks)} -> 清洗后: {len(final_blocks)}")


def main():
    # 获取脚本所在目录（根目录），然后在 input 子目录下查找
    script_dir = os.path.dirname(os.path.abspath(__file__))
    input_dir = os.path.join(script_dir, "input")

    # 删除所有 _fixed.srt 文件
    fixed_files = glob.glob(os.path.join(input_dir, "*_fixed.srt"))
    for f in fixed_files:
        os.remove(f)
    print("删除所有 _fixed.srt 文件")

    srt_files = glob.glob(os.path.join(input_dir, "*.srt"))
    for f in srt_files:
        if "_fixed" in f:
            continue
        process_srt(f)

    srt_files = glob.glob(os.path.join(input_dir, "*.srt"))
    for f in srt_files:
        if "_fixed" in f:
            continue
        os.remove(f)
    print("删除原始未处理的 .srt 文件")

    # remove _fixed suffix from all srt files
    srt_files = glob.glob(os.path.join(input_dir, "*.srt"))
    for f in srt_files:
        if "_fixed" in f:
            renamed_f = f.replace("_fixed", "")
            os.rename(f, renamed_f)


if __name__ == "__main__":
    main()
