import os
import re
from pathlib import Path


def remove_part_headers(markdown_file):
    """
    删除 markdown 文件中只包含 "## 第 \d* 部分" 的行
    """
    with open(markdown_file, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    # 匹配模式：只包含 "## 第 \d* 部分" 的行（可能前后有空格）
    pattern = re.compile(r'^\s*##\s+第\s+\d+\s+部分\s*$')

    filtered_lines = []
    for line in lines:
        if not pattern.match(line):
            filtered_lines.append(line)

    # 写回文件
    with open(markdown_file, 'w', encoding='utf-8') as f:
        f.writelines(filtered_lines)

    return len(lines) - len(filtered_lines)


def main():
    output_dir = Path('output')

    # 查找所有 markdown 文件
    md_files = list(output_dir.glob('*.md'))

    if not md_files:
        print("未找到 markdown 文件")
        return

    total_removed = 0
    for md_file in md_files:
        print(f"处理文件: {md_file}")
        removed = remove_part_headers(md_file)
        total_removed += removed
        print(f"  删除了 {removed} 行")

    print(f"\n总共删除了 {total_removed} 行")


if __name__ == '__main__':
    main()
