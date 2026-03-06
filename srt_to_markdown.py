import argparse
import os
import re


def is_timecode_line(line: str) -> bool:
    """
    判断一行是否是 SRT 时间轴行，例如：
    00:00:01,000 --> 00:00:04,000
    """
    line = line.strip()
    # 简单且通用的匹配：开头是数字且包含 "-->"
    if "-->" not in line:
        return False
    # 更严格的时间格式匹配
    timecode_pattern = re.compile(
        r"^\d{2}:\d{2}:\d{2},\d{3}\s+-->\s+\d{2}:\d{2}:\d{2},\d{3}"
    )
    return bool(timecode_pattern.match(line))


def srt_to_markdown_text(srt_path: str) -> str:
    """
    只去掉序号行和时间轴行，保留所有字幕文本和原有空行/换行。
    """
    with open(srt_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    output_lines = []
    for raw in lines:
        line = raw.rstrip("\n")  # 去掉结尾换行，方便判断内容类型
        stripped = line.strip()

        # 只包含空格/回车的“空行”直接丢弃
        if stripped == "":
            continue

        # 去掉纯数字序号行
        if stripped.isdigit():
            continue

        # 去掉时间轴行
        if is_timecode_line(stripped):
            continue

        # 其余行视为正文，保留
        output_lines.append(line)

    # 重新用 '\n' 拼接，并在末尾加一个换行，方便 markdown 编辑
    return "\n".join(output_lines).rstrip() + "\n"


def process_single_file(input_path: str, output_path: str | None = None):
    input_path = os.path.abspath(input_path)
    if not os.path.isfile(input_path):
        raise FileNotFoundError(f"找不到输入文件: {input_path}")

    if output_path:
        output_path = os.path.abspath(output_path)
    else:
        base, _ = os.path.splitext(input_path)
        output_path = base + ".md"

    md_text = srt_to_markdown_text(input_path)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(md_text)

    print(f"已生成 Markdown 文件: {output_path}")


def process_input_dir(input_dir: str):
    """
    批量处理 input 目录下所有 .srt 文件，逐个生成同名 .md。
    """
    input_dir = os.path.abspath(input_dir)
    if not os.path.isdir(input_dir):
        raise NotADirectoryError(f"找不到目录: {input_dir}")

    count = 0
    for name in os.listdir(input_dir):
        if not name.lower().endswith(".srt"):
            continue
        srt_path = os.path.join(input_dir, name)
        base, _ = os.path.splitext(srt_path)
        md_path = base + ".md"
        process_single_file(srt_path, md_path)
        count += 1

    print(f"共处理 {count} 个 SRT 文件。")


def main():
    parser = argparse.ArgumentParser(
        description="将 SRT 字幕转换为纯文本 Markdown（删除序号和时间轴，保留换行）。"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "-i",
        "--input",
        help="输入的 .srt 文件路径（单文件模式）",
    )
    group.add_argument(
        "-b",
        "--batch",
        action="store_true",
        help="批量模式：处理脚本目录下 input 目录中的所有 .srt 文件",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="输出 markdown 文件路径（仅单文件模式下有效，默认与输入同名，后缀改为 .md）",
    )

    args = parser.parse_args()

    # 批量模式：处理 ./input 下所有 .srt
    if args.batch:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        input_dir = os.path.join(script_dir, "input")
        process_input_dir(input_dir)
        return

    # 单文件模式
    process_single_file(args.input, args.output)


if __name__ == "__main__":
    main()

