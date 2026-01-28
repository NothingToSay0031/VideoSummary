#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量处理 input 文件夹下的本地 SRT 字幕：
- 不下载视频
- 不提取截图（text-only 模式）
- 调用现有的 VideoSummaryApp，生成每个 SRT 对应的总结 markdown

使用示例：
    python local_srt_only_batch.py
    python local_srt_only_batch.py -i input -o output_srt_only
    python local_srt_only_batch.py -i input -o output_srt_only --test
"""

from video_summary_app import VideoSummaryApp, sanitize_filename  # type: ignore
import os
import sys
import argparse
from typing import List

# 保证可以导入同目录下的 video_summary_app
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)


def find_srt_files(input_dir: str) -> List[str]:
    """递归查找目录下所有 .srt 和 .txt 文件"""
    subtitle_files: List[str] = []
    for root, _dirs, files in os.walk(input_dir):
        for name in files:
            if name.lower().endswith((".srt", ".txt")):
                subtitle_files.append(os.path.join(root, name))
    return sorted(subtitle_files)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="批量处理本地 SRT/TXT 字幕文件（仅生成文本总结，不截图）",
    )
    parser.add_argument(
        "-i",
        "--input-dir",
        default="input",
        help="SRT/TXT 所在输入目录（默认: input）",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        default="output",
        help="输出目录（默认: output）",
    )
    parser.add_argument(
        "-t",
        "--test",
        action="store_true",
        help="测试模式：不真正调用 LLM，而是把 Prompt 输出到中间文件",
    )

    args = parser.parse_args()

    input_dir = os.path.abspath(args.input_dir)
    output_dir = os.path.abspath(args.output_dir)

    if not os.path.isdir(input_dir):
        print(f"输入目录不存在: {input_dir}")
        sys.exit(1)

    subtitle_files = find_srt_files(input_dir)
    if not subtitle_files:
        print(f"在目录中未找到 .srt 或 .txt 文件: {input_dir}")
        sys.exit(0)

    print(f"发现 {len(subtitle_files)} 个字幕文件（SRT/TXT），将逐个生成总结（仅文本，无截图）")

    app = VideoSummaryApp(
        output_dir=output_dir,
        test_mode=args.test,
        text_only=True,      # 关键：仅文本模式，不提取截图
        cookies_file=None,
    )

    success = 0
    failed = 0

    for idx, subtitle_path in enumerate(subtitle_files, start=1):
        rel_path = os.path.relpath(subtitle_path, input_dir)
        base_name = os.path.splitext(os.path.basename(subtitle_path))[0]
        title = sanitize_filename(base_name)

        print(f"\n[{idx}/{len(subtitle_files)}] 处理字幕: {rel_path}")
        try:
            result_md = app.process_video(
                url=None,
                local_video=None,
                local_subtitle=subtitle_path,
                provided_title=title,
            )
            if result_md:
                print(f"  ✅ 完成: {os.path.relpath(result_md, output_dir)}")
                success += 1
            else:
                print("  ⚠️ 未生成输出文件（可能缺少有效内容）")
                failed += 1
        except Exception as exc:  # pylint: disable=broad-except
            print(f"  ❌ 失败: {exc}")
            failed += 1

    print("\n====== 批处理完成 ======")
    print(f"成功: {success} 个")
    print(f"失败: {failed} 个")


if __name__ == "__main__":
    main()
