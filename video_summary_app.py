#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
视频总结应用 - 完整的YouTube/Bilibili视频总结工具
输入视频链接，下载视频和字幕，生成AI总结并附带截图
"""

import os
import sys
import re
import subprocess
import json
import logging
import time
from datetime import datetime
from bisect import bisect_left
from urllib.parse import quote
from typing import List, Dict, Any, Tuple, TextIO, Optional
from concurrent.futures import ThreadPoolExecutor
import cv2
import numpy as np

try:
    from google import genai
except ImportError:
    genai = None

INVALID_PATH_CHARS = set('<>:"/\\|?*')


def sanitize_filename(name: str) -> str:
    """
    将不适合作为文件/文件夹名的字符替换为下划线
    """
    if not name:
        return "untitled"

    sanitized_chars = []
    for ch in name:
        if ch in INVALID_PATH_CHARS or ord(ch) < 32 or ch.isspace():
            sanitized_chars.append('_')
        else:
            sanitized_chars.append(ch)

    sanitized = ''.join(sanitized_chars)
    sanitized = re.sub(r'_+', '_', sanitized).strip('_')
    return sanitized or "untitled"


def chinese_char_ratio(text: str) -> float:
    """
    统计文本中汉字所占比例（忽略空白字符）
    """
    if not text:
        return 0.0
    total_chars = len([ch for ch in text if not ch.isspace()])
    if total_chars == 0:
        return 0.0
    chinese_count = len(re.findall(r'[\u4e00-\u9fff]', text))
    return chinese_count / total_chars


# ==== LLM 配置（可根据需要修改）====
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
PRIMARY_MODEL = "gemini-2.5-pro"
BASE_SYSTEM_PROMPT = """
## Background Information

You are an expert proficient in computer graphics, skilled at explaining complex technical concepts in a clear, structured manner to people who have a foundation in graphics but wish to delve deeper into the content of a video lecture.
You are reading through and summarizing a long technical graphics lecture transcript section by section. This is part ${current}$ of ${total}$ total parts.
My Goal: I am a **Game Engine Engineer and Rendering Engineer** with a **graphics background**. I aim to enrich my knowledge, gain in-depth mastery of graphics and game engine knowledge, and understand industry developments, thus seeking to **deeply study the content related to the lecture**.

## Task Requirements

1.  **Core Task:** Please summarize the content I provide below into **easily understandable and memorable notes**.
2.  **Formatting Requirements:**
    * Use a **clear hierarchical structure** (main headings, subheadings, bullet points).
    * For each topic, distill and **bold** the **core concepts** and **key terminology**.
    * If **formulas or important algorithms** are involved, please highlight them. Use standard **LaTeX format** to ensure the document can be rendered correctly.
    * The language style should be as **accessible as possible**, avoiding direct copying of obscure jargon from the original text.
    * **Strictly No Meta-talk:**
        * **Absolutely prohibited** to output any opening or closing remarks (e.g., "...Here are the notes prepared for you...").
        * **Absolutely prohibited** to include metadata titles like "Part ${current}$" or "Part X" in the body text.
        * **Start directly with the technical content title.**
3. 输出中文！
## Content to be Summarized
"""

# ==== 字幕解析逻辑====
SubtitleEntry = Dict[str, str]
SubtitleData = List[SubtitleEntry]


# ==== 字幕去重逻辑====
def is_timecode(line: str) -> bool:
    """判断一行是否是时间轴"""
    return "-->" in line and line[0].isdigit()


def parse_srt_robust(file_path: str) -> List[Dict[str, Any]]:
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

        # 核心逻辑：扫描到时间轴，说明抓到了一个新块的"骨架"
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


def get_longest_overlap(s1: str, s2: str) -> int:
    """计算重叠长度逻辑"""
    if not s1 or not s2:
        return 0
    min_overlap = 4
    max_possible = min(len(s1), len(s2))
    for length in range(max_possible, min_overlap - 1, -1):
        if s1.endswith(s2[:length]):
            return length
    return 0


def remove_duplicates_from_srt(file_path: str) -> bool:
    """
    去除 SRT 字幕文件中的重复内容
    
    Args:
        file_path: 字幕文件路径
        
    Returns:
        是否成功处理（True）或失败（False）
    """
    try:
        logger.info(f"🧹 开始对字幕进行去重处理: {os.path.basename(file_path)}")
        blocks = parse_srt_robust(file_path)

        if not blocks:
            logger.warning("  -> 空文件或解析失败，跳过去重")
            return False

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

        # 写入原文件（覆盖）
        with open(file_path, 'w', encoding='utf-8') as f:
            for index, (time, text) in enumerate(final_blocks, 1):
                f.write(f"{index}\n{time}\n{text}\n\n")

        logger.info(
            f"  ✅ 去重完成! 原始行数: {len(blocks)} -> 清洗后: {len(final_blocks)}")
        return True
    except Exception as e:
        logger.error(f"  ❌ 字幕去重失败: {e}")
        return False


def parse_subtitles(file_content: str) -> Tuple[SubtitleData, str]:
    """
    解析 SRT 字幕，返回结构化字幕列表与整合文本
    """
    if file_content.startswith('\ufeff'):
        file_content = file_content.lstrip('\ufeff')
    if file_content.startswith('WEBVTT'):
        file_content = re.sub(r'WEBVTT.*?\n\n', '',
                              file_content, flags=re.DOTALL)

    blocks = file_content.strip().split('\n\n')
    subtitle_data: SubtitleData = []
    consolidated_lines: List[str] = []
    timestamp_pattern = re.compile(
        r'(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,\.]\d{3})')

    for block in blocks:
        lines = block.strip().split('\n')
        if not lines:
            continue

        time_match = None
        dialogue_lines: List[str] = []

        for line in lines:
            line = line.strip()
            if '-->' in line and timestamp_pattern.search(line):
                time_match = timestamp_pattern.search(line)
            elif line.isdigit():
                continue
            else:
                dialogue_lines.append(line)

        if time_match and dialogue_lines:
            start_time = time_match.group(1).replace('.', ',')
            end_time = time_match.group(2).replace('.', ',')
            full_dialogue = ' '.join(dialogue_lines)
            subtitle_data.append({
                'start': start_time,
                'end': end_time,
                'text': full_dialogue
            })
            consolidated_lines.append(full_dialogue)

    consolidated_text = '\n'.join(consolidated_lines)
    return subtitle_data, consolidated_text


def detect_language(content: str, chinese_threshold: float = 0.1) -> str:
    """检测文本主要语言，默认中文字符比例≥阈值视为中文"""
    total_chars = len(content)
    if total_chars == 0:
        return "Unknown"

    chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', content))
    chinese_ratio = chinese_chars / total_chars
    language = "Chinese" if chinese_ratio >= chinese_threshold else "English"
    logger.info(f"🌐 检测语言: {language} (中文比例: {chinese_ratio:.2%})")
    return language


def generate_chunk_summary(client, chunk_text: str, current_idx: int,
                           total_chunks: int, model_name: str = PRIMARY_MODEL) -> str:
    """
    调用 Gemini 模型生成单个片段的总结
    """
    if client is None:
        raise RuntimeError("genai client 未初始化")

    prompt = BASE_SYSTEM_PROMPT.format(
        current=current_idx, total=total_chunks) + "\n\n" + chunk_text

    logger.info(
        f"   >>> LLM 总结第 {current_idx}/{total_chunks} 片段，长度 {len(chunk_text)} 字")
    response = client.models.generate_content(
        model=model_name,
        contents=prompt,
    )
    return response.text


# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class VideoDownloader:
    """视频和字幕下载器（使用yt-dlp）"""

    def __init__(self, output_dir: str = "downloads", cookies_file: str = None):
        """
        初始化下载器

        Args:
            output_dir: 下载文件保存目录
            cookies_file: Cookies 文件路径（用于 Bilibili 等需要登录的网站）
        """
        self.output_dir = output_dir
        self.cookies_file = cookies_file
        os.makedirs(output_dir, exist_ok=True)

    def _build_ytdlp_command(self, base_args: List[str]) -> List[str]:
        """
        构建 yt-dlp 命令，自动添加 cookies 参数（如果提供）

        Args:
            base_args: yt-dlp 的基础参数列表（不包含 'yt-dlp'）

        Returns:
            完整的命令列表
        """
        cmd = ['yt-dlp']
        if self.cookies_file:
            if os.path.exists(self.cookies_file):
                cmd.extend(['--cookies', self.cookies_file])
                logger.info(f"🍪 使用 cookies 文件: {self.cookies_file}")
            else:
                logger.warning(
                    f"⚠️  Cookies 文件不存在: {self.cookies_file}，将不使用 cookies")
        cmd.extend(base_args)
        return cmd

    def download(self, url: str, download_video: bool = True) -> Dict[str, str]:
        """
        下载视频和字幕

        Args:
            url: 视频链接（YouTube/Bilibili等）
            download_video: 是否下载视频文件

        Returns:
            包含视频路径和字幕路径的字典
            {
                'video': '视频文件路径',
                'subtitle': '字幕文件路径' 或 None,
                'title': '视频标题'
            }
        """
        logger.info(f"开始下载: {url}")

        # 检查yt-dlp是否安装
        try:
            subprocess.run(['yt-dlp', '--version'],
                           capture_output=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            logger.error("错误: 未找到 yt-dlp，请先安装: pip install yt-dlp")
            raise

        # 设置输出模板
        video_template = os.path.join(self.output_dir, '%(title)s.%(ext)s')
        subtitle_template = os.path.join(
            self.output_dir, '%(title)s.%(language)s.%(ext)s')

        # 首先获取视频信息
        info_cmd = self._build_ytdlp_command([
            '--dump-json',
            '--skip-download',
            url
        ])

        raw_video_title = None
        try:
            info_output = subprocess.run(
                info_cmd, capture_output=True, text=True, check=True
            )
            video_info = json.loads(info_output.stdout)
            raw_video_title = video_info.get('title', 'video')
            logger.info(f"📹 检测到视频标题: {raw_video_title}")
            # 清理标题中的非法字符
            video_title = sanitize_filename(raw_video_title)
            logger.info(f"📝 清理后的标题: {video_title}")
        except Exception as e:
            logger.warning(f"获取视频信息失败: {e}，使用默认标题")
            raw_video_title = 'video'
            video_title = 'video'

        chinese_ratio = chinese_char_ratio(raw_video_title)
        prefer_chinese = chinese_ratio > 0.3
        if prefer_chinese:
            logger.info(
                f"🌏 检测到视频标题中中文比例 {chinese_ratio:.0%}，优先选择中文字幕")
        else:
            logger.info(
                f"🌐 中文比例 {chinese_ratio:.0%}，默认优先英文字幕")

        video_path = None
        # 检查本地是否已有视频和字幕文件
        logger.info("检查本地是否已有视频和字幕文件...")
        existing_video_path = None
        existing_subtitle_path = None

        video_extensions = ['.mp4', '.mkv', '.webm', '.flv', '.avi']
        if download_video:
            # 查找本地视频文件（匹配标题）
            for ext in video_extensions:
                potential_video = os.path.join(
                    self.output_dir, f"{video_title}{ext}")
                if os.path.exists(potential_video) and os.path.getsize(potential_video) > 0:
                    existing_video_path = potential_video
                    logger.info(
                        f"✅ 找到本地视频文件: {os.path.basename(existing_video_path)}")
                    break

            # 如果精确匹配没找到，尝试模糊匹配
            if not existing_video_path:
                title_clean = video_title.replace(' ', '_').replace('/', '_')
                title_lower = video_title.lower()
                title_clean_lower = title_clean.lower()
                # 提取标题中的关键词（长度>2的单词）
                title_words = [w for w in re.split(
                    r'[\s_\-]+', title_lower) if len(w) > 2]

                for f in os.listdir(self.output_dir):
                    if f.endswith(tuple(video_extensions)) and not f.endswith(('.srt', '.vtt')):
                        f_lower = f.lower()
                        # 检查是否包含完整标题或清理后的标题
                        if title_lower in f_lower or title_clean_lower in f_lower:
                            potential_path = os.path.join(self.output_dir, f)
                            if os.path.getsize(potential_path) > 0:
                                existing_video_path = potential_path
                                logger.info(
                                    f"✅ 找到本地视频文件（模糊匹配）: {os.path.basename(existing_video_path)}")
                                break
                        # 或者检查是否包含标题中的多个关键词（至少2个）
                        elif len(title_words) >= 2:
                            matched_words = sum(
                                1 for word in title_words if word in f_lower)
                            if matched_words >= 2:  # 至少匹配2个关键词
                                potential_path = os.path.join(
                                    self.output_dir, f)
                                if os.path.getsize(potential_path) > 0:
                                    existing_video_path = potential_path
                                    logger.info(
                                        f"✅ 找到本地视频文件（关键词匹配，{matched_words}/{len(title_words)}）: {os.path.basename(existing_video_path)}")
                                    break

        # 查找本地字幕文件（容忍 B 站/YouTube 扩展命名，例如 *.NA.ai-zh.srt）
        existing_subtitle_path = self._find_local_subtitle_file(video_title)
        if existing_subtitle_path:
            logger.info(
                f"✅ 找到本地字幕文件: {os.path.basename(existing_subtitle_path)}")

        # 检查可用字幕
        subtitle_lang = None
        available_subs = ''
        try:
            sub_cmd = self._build_ytdlp_command([
                '--list-subs',
                '--skip-download',
                url
            ])
            sub_output = subprocess.run(
                sub_cmd, capture_output=True, text=True, check=True
            )
            available_subs = sub_output.stdout

            zh_first_preferences = [
                (r'\bai-zh\b', 'ai-zh', "找到简体中文字幕 ai-zh"),
                (r'\b(zh-cn|zh_CN|chinese)\b', 'zh-cn', "找到简体中文字幕"),
                (r'\b(zh-tw|zh_TW)\b', 'zh-tw', "找到繁体中文字幕"),
                (r'\bzh\b', 'zh', "找到中文字幕"),
                (r'\bai-en\b', 'ai-en', "找到英文字幕 ai-en"),
                (r'\b(en|english)\b', 'en', "找到英文字幕")
            ]
            en_first_preferences = [
                (r'\bai-en\b', 'ai-en', "找到英文字幕 ai-en"),
                (r'\b(en|english)\b', 'en', "找到英文字幕"),
                (r'\bai-zh\b', 'ai-zh', "找到简体中文字幕 ai-zh"),
                (r'\b(zh-cn|zh_CN|chinese)\b', 'zh-cn', "找到简体中文字幕"),
                (r'\b(zh-tw|zh_TW)\b', 'zh-tw', "找到繁体中文字幕"),
                (r'\bzh\b', 'zh', "找到中文字幕")
            ]

            lang_preferences = zh_first_preferences if prefer_chinese else en_first_preferences

            for pattern, code, message in lang_preferences:
                if re.search(pattern, available_subs, re.IGNORECASE):
                    subtitle_lang = code
                    logger.info(message)
                    break

            if not subtitle_lang:
                logger.warning("未找到中文或英文字幕，将尝试下载所有可用字幕")
                subtitle_lang = 'all'  # 下载所有字幕，后续选择
        except Exception as e:
            logger.warning(f"检查字幕失败: {e}，将尝试下载所有字幕")
            subtitle_lang = 'all'

        # 如果已有本地视频，跳过下载
        if download_video:
            if existing_video_path:
                logger.info("⏭️  跳过视频下载，使用本地文件")
                video_path = existing_video_path
            else:
                # 下载视频（最高画质，不下载音频，因为只用于截图）
                logger.info("正在下载视频（最高画质，无音频，仅用于截图）...")
                # 只下载视频流（最高画质），不下载音频
                video_cmd = self._build_ytdlp_command([
                    # 优先mp4，其次720p+，最后任何最高画质视频
                    '-f', 'bestvideo[ext=mp4]/bestvideo[height>=720]/bestvideo',
                    '--no-write-subs',  # 不下载字幕（我们会单独下载）
                    '--no-playlist',  # 不下载播放列表
                    '-o', video_template,
                    url
                ])

                try:
                    result = subprocess.run(
                        video_cmd, check=True, capture_output=True, text=True)
                    # 等待文件写入完成
                    import time
                    time.sleep(1)
                except subprocess.CalledProcessError as e:
                    # 如果只下载视频失败，尝试下载视频+最低音频
                    logger.warning(
                        f"只下载视频失败: {e.stderr if hasattr(e, 'stderr') and e.stderr else str(e)}")
                    logger.info("尝试下载视频+最低音频...")
                    video_cmd_fallback = self._build_ytdlp_command([
                        '-f', 'bestvideo[ext=mp4]+worstaudio[ext=m4a]/bestvideo+worstaudio',
                        '--no-write-subs',
                        '--no-playlist',
                        '-o', video_template,
                        url
                    ])
                    try:
                        subprocess.run(video_cmd_fallback, check=True,
                                       capture_output=True, text=True)
                        import time
                        time.sleep(1)
                    except Exception as e2:
                        logger.error(f"视频下载失败: {e2}")
                        raise

                # 查找下载的视频文件（优先匹配当前视频标题）
                video_path = None
                title_clean = video_title.replace(' ', '_').replace('/', '_')

                # 1. 优先精确匹配：标题+扩展名
                for ext in ['.mp4', '.mkv', '.webm', '.flv', '.avi']:
                    potential_video = os.path.join(
                        self.output_dir, f"{video_title}{ext}")
                    if os.path.exists(potential_video) and os.path.getsize(potential_video) > 0:
                        video_path = potential_video
                        logger.info(
                            f"✅ 找到下载的视频文件（精确匹配）: {os.path.basename(video_path)}")
                        break

                # 2. 如果精确匹配没找到，尝试模糊匹配当前视频标题
                if not video_path:
                    matching_files = []
                    for f in os.listdir(self.output_dir):
                        if f.endswith(('.mp4', '.mkv', '.webm', '.flv', '.avi')) and not f.endswith(('.srt', '.vtt')):
                            f_lower = f.lower()
                            title_lower = video_title.lower()
                            title_clean_lower = title_clean.lower()
                            # 检查文件名是否包含视频标题
                            if (title_lower in f_lower or title_clean_lower in f_lower or
                                    any(part for part in title_lower.split() if len(part) > 3 and part in f_lower)):
                                matching_files.append(f)

                    if matching_files:
                        # 选择匹配文件中最大的（通常是刚下载的）
                        matching_files_with_size = [(f, os.path.getsize(os.path.join(self.output_dir, f)))
                                                    for f in matching_files]
                        matching_files_with_size.sort(
                            key=lambda x: x[1], reverse=True)
                        video_path = os.path.join(
                            self.output_dir, matching_files_with_size[0][0])
                        logger.info(
                            f"✅ 找到下载的视频文件（模糊匹配）: {os.path.basename(video_path)}")

                # 3. 如果还是没找到，尝试找最近修改的文件（可能是刚下载的）
                if not video_path:
                    video_files = []
                    for f in os.listdir(self.output_dir):
                        if f.endswith(('.mp4', '.mkv', '.webm', '.flv', '.avi')) and not f.endswith(('.srt', '.vtt')):
                            video_files.append(f)

                    if video_files:
                        # 按修改时间排序，选择最新的文件
                        video_files_with_time = []
                        for f in video_files:
                            file_path = os.path.join(self.output_dir, f)
                            mtime = os.path.getmtime(file_path)
                            video_files_with_time.append(
                                (f, mtime, os.path.getsize(file_path)))

                        video_files_with_time.sort(
                            key=lambda x: x[1], reverse=True)  # 按修改时间降序
                        video_path = os.path.join(
                            self.output_dir, video_files_with_time[0][0])
                        logger.warning(
                            f"⚠️  无法精确匹配视频标题，使用最近修改的文件: {os.path.basename(video_path)}")
                        logger.warning(f"   请确认这是正确的视频文件！")

                if not video_path:
                    raise FileNotFoundError(f"未找到下载的视频文件（标题: {video_title}）")

                file_size = os.path.getsize(video_path) / 1024 / 1024
                logger.info(
                    f"视频文件: {os.path.basename(video_path)} ({file_size:.2f} MB)")
        else:
            logger.info("📝 Non video模式：跳过视频下载")

        # 如果已有本地字幕，跳过下载
        if existing_subtitle_path:
            logger.info("⏭️  跳过字幕下载，使用本地文件")
            subtitle_path = existing_subtitle_path
        else:
            # 下载字幕（如果可用，只下载srt格式）
            subtitle_path = None
            if subtitle_lang and subtitle_lang != 'all':
                logger.info(f"正在下载字幕 ({subtitle_lang})，仅SRT格式...")
                subtitle_cmd = self._build_ytdlp_command([
                    '--write-subs',
                    '--write-auto-subs',  # 也下载自动生成的字幕
                    '--sub-langs', subtitle_lang,
                    '--sub-format', 'srt',  # 只下载srt格式
                    '--skip-download',
                    '-o', subtitle_template,
                    url
                ])

                try:
                    subprocess.run(subtitle_cmd, check=True,
                                   capture_output=True)
                except Exception as e:
                    logger.warning(f"字幕下载失败: {e}")

            # 如果subtitle_lang是'all'，尝试下载所有字幕（仅srt格式）
            if subtitle_lang == 'all':
                logger.info("尝试下载所有可用字幕（仅SRT格式）...")
                try:
                    subtitle_cmd = self._build_ytdlp_command([
                        '--write-subs',
                        '--write-auto-subs',
                        '--sub-format', 'srt',  # 只下载srt格式
                        '--skip-download',
                        '-o', subtitle_template,
                        url
                    ])
                    subprocess.run(subtitle_cmd, check=True,
                                   capture_output=True)
                except Exception as e:
                    logger.warning(f"下载所有字幕失败: {e}")

            # 查找字幕文件（优先中文，其次英文，仅srt格式）
            if subtitle_lang:
                subtitle_path = self._find_local_subtitle_file(video_title)
                if subtitle_path:
                    logger.info(
                        f"选择字幕文件: {os.path.basename(subtitle_path)}")
                    # 对下载的字幕进行去重处理
                    if subtitle_path.endswith('.srt'):
                        remove_duplicates_from_srt(subtitle_path)

        return {
            'video': video_path,
            'subtitle': subtitle_path,
            'title': video_title
        }

    @staticmethod
    def _normalize_for_match(text: str) -> str:
        """将标题/文件名标准化，便于模糊匹配"""
        text = text.lower()
        text = re.sub(r'\.na', '.', text)  # B站字幕会出现 .NA
        return re.sub(r'[\s_\-\.]+', '', text)

    @staticmethod
    def _subtitle_lang_priority(filename: str) -> int:
        """字幕语言优先级：ai-zh > zh > ai-en > en > other"""
        name = filename.lower()
        if 'ai-zh' in name:
            return 5
        if any(tag in name for tag in ['zh-cn', 'zh_tw', 'zh', 'chinese', '中文', 'cn']):
            return 4
        if 'ai-en' in name:
            return 3
        if any(tag in name for tag in ['en', 'english', '英文']):
            return 2
        return 1

    def _find_local_subtitle_file(self, video_title: str) -> str:
        """
        在输出目录中查找最匹配的视频字幕文件，容忍 B 站的 .NA/语言后缀
        """
        normalized_title_full = video_title.lower()
        normalized_title_simple = self._normalize_for_match(video_title)
        title_words = [w for w in re.split(
            r'[\s_\-]+', normalized_title_full) if len(w) > 2]

        best_candidate = None
        best_score = -1

        for filename in os.listdir(self.output_dir):
            if not filename.lower().endswith('.srt'):
                continue
            file_path = os.path.join(self.output_dir, filename)
            if os.path.getsize(file_path) <= 0:
                continue

            fname_lower = filename.lower()
            fname_simple = self._normalize_for_match(
                os.path.splitext(fname_lower)[0])

            match_score = 0
            if normalized_title_full in fname_lower or normalized_title_simple in fname_simple:
                match_score = 2
            else:
                matched_words = sum(
                    1 for word in title_words if word and word in fname_lower)
                if matched_words >= max(1, len(title_words) // 2):
                    match_score = 1

            if match_score == 0:
                continue

            lang_score = self._subtitle_lang_priority(fname_lower)
            total_score = lang_score * 10 + match_score

            if total_score > best_score:
                best_score = total_score
                best_candidate = file_path
            elif total_score == best_score and best_candidate:
                if os.path.getmtime(file_path) > os.path.getmtime(best_candidate):
                    best_candidate = file_path

        return best_candidate


class TimeRangeExtractor:
    """时间段提取器 - 在指定时间段内提取视频帧"""

    @staticmethod
    def time_str_to_seconds(time_str: str) -> float:
        """
        将时间字符串转换为秒数
        支持格式: HH:MM:SS,mmm 或 HH:MM:SS.mmm
        """
        # 替换逗号为点
        time_str = time_str.replace(',', '.')

        # 解析时间
        parts = time_str.split(':')
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = float(parts[2])

        return hours * 3600 + minutes * 60 + seconds

    @staticmethod
    def seconds_to_time_str(seconds: float) -> str:
        """将秒数转换为 HH:MM:SS 格式"""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    @staticmethod
    def extract_frames_in_range(video_path: str, start_time: float, end_time: float,
                                output_dir: str, interval: float = 2.0,
                                image_format: str = 'jpg', quality: int = 95,
                                skip_similar: bool = True,
                                similarity_threshold: float = 0.95,
                                # 新增：分块 + 主/辅关键帧 + 去抖动相关参数（保持有默认值，原有调用不受影响）
                                use_block_diff: bool = True,
                                primary_change_threshold: float = 0.25,
                                secondary_change_threshold: float = 0.12,
                                min_primary_interval: float = 4.0,
                                min_secondary_interval: float = 2.0,
                                block_grid_rows: int = 4,
                                block_grid_cols: int = 4) -> List[str]:
        """
        在指定时间段内提取视频帧

        Args:
            video_path: 视频文件路径
            start_time: 开始时间（秒）
            end_time: 结束时间（秒）
            output_dir: 输出目录
            interval: 提取间隔（秒）
            image_format: 图片格式
            quality: 图片质量
            skip_similar: 是否启用智能去重（保留相似场景的最后一帧）

        Returns:
            提取的图片文件路径列表
        """
        os.makedirs(output_dir, exist_ok=True)

        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            raise ValueError(f"无法打开视频文件: {video_path}")

        fps = cap.get(cv2.CAP_PROP_FPS)
        start_frame = int(start_time * fps)
        end_frame = int(end_time * fps)
        frame_interval = max(1, int(fps * interval))

        # 设置图片编码参数
        if image_format.lower() == 'jpg' or image_format.lower() == 'jpeg':
            encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), quality]
            ext = 'jpg'
        elif image_format.lower() == 'png':
            encode_param = [int(cv2.IMWRITE_PNG_COMPRESSION), 3]
            ext = 'png'
        else:
            encode_param = []
            ext = image_format.lower()

        extracted_files = []
        frame_count = 0
        extracted_count = 0
        skipped_similar = 0

        def save_frame(pending, timestamp, is_primary: bool = True):
            nonlocal extracted_count
            if pending is None:
                return
            time_str = TimeRangeExtractor.seconds_to_time_str(timestamp)
            # 标记主关键帧 / 辅助帧，方便后续人工查看（对后续流程无破坏性影响）
            kind = "main" if is_primary else "aux"
            filename = f"frame_{kind}_{extracted_count:03d}_{time_str.replace(':', '')}.{ext}"
            filepath = os.path.join(output_dir, filename)
            if encode_param:
                cv2.imwrite(filepath, pending, encode_param)
            else:
                cv2.imwrite(filepath, pending)
            extracted_files.append(filepath)
            extracted_count += 1

        # 跳转到开始位置
        cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
        # 新逻辑：记录上一次真正保存的帧（用于比较变化），而不是仅仅“最后一帧”
        last_saved_frame = None
        last_saved_time: Optional[float] = None

        while frame_count <= (end_frame - start_frame):
            ret, frame = cap.read()

            if not ret:
                break

            # 检查是否超出结束时间
            current_time = start_time + (frame_count / fps)
            if current_time > end_time:
                break

            # 按间隔提取
            if frame_count % frame_interval == 0:
                # 不做智能去重时，仍按固定间隔直接保存帧
                if not skip_similar:
                    save_frame(frame, current_time, is_primary=True)
                else:
                    # 分块 + 去抖动 + 主/辅关键帧逻辑
                    # 1) 第一帧：无论如何先保存为主关键帧，作为基准
                    if last_saved_frame is None:
                        save_frame(frame, current_time, is_primary=True)
                        last_saved_frame = frame.copy()
                        last_saved_time = current_time
                    else:
                        # 计算变化程度：默认采用分块灰度平均差
                        if use_block_diff:
                            change_score = TimeRangeExtractor._block_change_score(
                                last_saved_frame, frame,
                                grid_rows=block_grid_rows,
                                grid_cols=block_grid_cols
                            )
                            # change_score 范围近似在 [0,1]，越大变化越明显
                        else:
                            # 退化为旧的全局相似度，再转成“变化分数”
                            similarity = TimeRangeExtractor._calculate_similarity(
                                last_saved_frame, frame)
                            change_score = 1.0 - similarity

                        # 与上一次关键帧的时间间隔，作为“去抖动”的最小间隔
                        time_since_last = (current_time - last_saved_time) if last_saved_time is not None else float(
                            "inf")

                        is_primary = False
                        should_save = False

                        # 主关键帧：变化较大，且距离上一次关键帧间隔够长
                        if change_score >= primary_change_threshold and time_since_last >= min_primary_interval:
                            is_primary = True
                            should_save = True
                        # 辅助关键帧：变化中等，但也需要一定时间间隔，避免同一内容密集截图
                        elif change_score >= secondary_change_threshold and time_since_last >= min_secondary_interval:
                            is_primary = False
                            should_save = True
                        else:
                            # 变化太小或时间间隔太短，都认为是抖动/细节变化，跳过
                            skipped_similar += 1

                        if should_save:
                            save_frame(frame, current_time,
                                       is_primary=is_primary)
                            last_saved_frame = frame.copy()
                            last_saved_time = current_time

            frame_count += 1

        cap.release()

        if skip_similar and skipped_similar > 0:
            logger.info(f"    跳过相似/抖动帧: {skipped_similar}")

        return extracted_files

    @staticmethod
    def _calculate_similarity(frame1, frame2) -> float:
        """计算两帧之间的相似度（使用缩放后的MSE）"""
        gray1 = cv2.cvtColor(frame1, cv2.COLOR_BGR2GRAY)
        gray2 = cv2.cvtColor(frame2, cv2.COLOR_BGR2GRAY)

        small_size = (128, 72)
        gray1_small = cv2.resize(gray1, small_size)
        gray2_small = cv2.resize(gray2, small_size)

        mse = np.mean(
            (gray1_small.astype(float) - gray2_small.astype(float)) ** 2)
        max_mse = 255.0 ** 2
        similarity = 1.0 - (mse / max_mse)
        return similarity

    @staticmethod
    def _block_change_score(frame1, frame2, grid_rows: int = 4, grid_cols: int = 4) -> float:
        # 1. 预处理
        gray1 = cv2.cvtColor(frame1, cv2.COLOR_BGR2GRAY)
        gray2 = cv2.cvtColor(frame2, cv2.COLOR_BGR2GRAY)

        # 统一缩放到较小尺寸 (确保能被 grid 整除，方便 reshape)
        target_w = 256
        target_h = 144

        # 简单的防御性检查，防止 grid 设置过大
        if target_h % grid_rows != 0 or target_w % grid_cols != 0:
            # 为了向量化性能，强行 resize 到能整除的大小
            target_h = (target_h // grid_rows) * grid_rows
            target_w = (target_w // grid_cols) * grid_cols

        gray1 = cv2.resize(gray1, (target_w, target_h)).astype(np.float32)
        gray2 = cv2.resize(gray2, (target_w, target_h)).astype(np.float32)

        # 2. 计算绝对差值图 (Global Difference Map)
        diff = np.abs(gray1 - gray2) / 255.0

        # 3. 向量化分块 (Magic happens here)
        # 将 (H, W) 重塑为 (GridRows, BlockH, GridCols, BlockW)
        # 然后交换轴变为 (GridRows, GridCols, BlockH, BlockW)
        block_h = target_h // grid_rows
        block_w = target_w // grid_cols

        reshaped = diff.reshape(grid_rows, block_h, grid_cols, block_w)
        # 交换轴，把块内的像素维度放在最后
        reshaped = reshaped.transpose(0, 2, 1, 3)

        # 4. 计算每个块的均值
        # axis=(2, 3) 意味着对每个块内部的所有像素求平均
        block_scores = reshaped.mean(axis=(2, 3))

        # block_scores 现在是一个 shape 为 (rows, cols) 的矩阵

        # 5. 决策策略：
        # 策略 A: 仍然返回全局平均 (和你之前的逻辑一样，但快很多)
        # return float(np.mean(block_scores))

        # 策略 B (推荐): 返回最大的局部变化。
        # 这样即使只有画面一角变了，分数也会很高。
        return float(np.max(block_scores))

class VideoSummaryApp:
    """视频总结应用主类"""

    def __init__(self, output_dir: str = "output", test_mode: bool = False,
                 text_only: bool = False, cookies_file: str = None):
        """
        初始化应用

        Args:
            output_dir: 输出目录
            test_mode: 是否启用测试模式（不调用LLM，仅输出Prompt）
            text_only: Non video模式，仅生成文本总结
            cookies_file: Cookies 文件路径（用于 Bilibili 等需要登录的网站）
        """
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)
        self.downloader = VideoDownloader(
            os.path.join(output_dir, "downloads"), cookies_file=cookies_file)
        self.time_extractor = TimeRangeExtractor()
        self.test_mode = test_mode
        self.text_only = text_only

    def process_video(self, url: Optional[str] = None,
                      frame_extraction_interval: float = 2.0,
                      skip_similar_frames: bool = True,
                      local_video: Optional[str] = None,
                      local_subtitle: Optional[str] = None,
                      provided_title: Optional[str] = None) -> Optional[str]:
        """
        处理视频：下载/加载、解析、总结、提取帧、生成markdown

        Args:
            url: 视频链接
            frame_extraction_interval: 帧提取间隔（秒）
            skip_similar_frames: 是否跳过相似帧
            local_video: 本地视频文件路径
            local_subtitle: 本地字幕文件路径（SRT）
            provided_title: 手动指定输出标题

        Returns:
            生成的markdown文件路径；若缺少字幕无法继续则返回 None
        """
        logger.info("=" * 60)
        logger.info("开始处理视频")
        logger.info("=" * 60)

        logger.info("\n[步骤 1/5] 准备视频和字幕...")

        download_result: Optional[Dict[str, str]] = None
        video_path: Optional[str] = None
        subtitle_path: Optional[str] = None

        video_title = sanitize_filename(
            provided_title) if provided_title else None

        if local_video:
            if not os.path.isfile(local_video):
                raise FileNotFoundError(f"本地视频文件不存在: {local_video}")
            video_path = local_video
            logger.info(f"🗂️ 使用本地视频: {video_path}")
            if url:
                logger.info("📥 将使用提供的 URL 下载字幕，搭配本地视频处理")
            if not video_title:
                base = os.path.splitext(os.path.basename(local_video))[0]
                video_title = sanitize_filename(base)

        if local_subtitle:
            if not os.path.isfile(local_subtitle):
                raise FileNotFoundError(f"本地字幕文件不存在: {local_subtitle}")
            subtitle_path = local_subtitle
            logger.info(f"🗂️ 使用本地字幕: {subtitle_path}")
            if not video_title:
                base = os.path.splitext(os.path.basename(local_subtitle))[0]
                video_title = sanitize_filename(base)

        if not subtitle_path:
            if not url:
                raise ValueError("未提供视频链接或字幕文件，无法继续")
            need_video_download = (
                not self.text_only and video_path is None
            )
            download_result = self.downloader.download(
                url, download_video=need_video_download)
            video_path = video_path or download_result.get('video')
            subtitle_path = download_result.get('subtitle')
            if not video_title:
                video_title = download_result.get('title', 'video')
        else:
            if url:
                logger.info("⚠️ 已指定本地字幕，将跳过字幕下载")

        if not subtitle_path:
            logger.warning("⚠️ 仅获取到视频文件，未找到字幕，终止本次处理")
            return None

        if not self.text_only:
            if not video_path:
                if not url:
                    raise ValueError("非 text-only 模式需要提供视频文件或链接")
                if not download_result:
                    download_result = self.downloader.download(
                        url, download_video=True)
                video_path = download_result.get('video')
            if not video_path or not os.path.isfile(video_path):
                raise FileNotFoundError("未找到可用的视频文件，无法提取截图")
        else:
            if not video_path and download_result:
                video_path = download_result.get('video')

        if not video_title:
            if video_path:
                video_title = sanitize_filename(
                    os.path.splitext(os.path.basename(video_path))[0])
            elif subtitle_path:
                video_title = sanitize_filename(
                    os.path.splitext(os.path.basename(subtitle_path))[0])
            else:
                video_title = "video"

        # 2. 解析字幕
        logger.info("\n[步骤 2/5] 解析字幕...")
        with open(subtitle_path, 'r', encoding='utf-8') as f:
            subtitle_content = f.read()

        subtitle_data, consolidated_text = parse_subtitles(subtitle_content)
        logger.info(f"解析完成: 共 {len(subtitle_data)} 条字幕")

        # 保存文稿到临时文件
        temp_text_file = os.path.join(
            self.output_dir, f"{video_title}_transcript.txt")
        with open(temp_text_file, 'w', encoding='utf-8') as f:
            f.write(consolidated_text)
        logger.info(f"文稿已保存: {temp_text_file}")

        # 3 & 4. AI总结 与 关键帧提取（并行）
        logger.info("\n[步骤 3/5] 生成AI总结...")
        if not self.text_only:
            logger.info("\n[步骤 4/5] 提取关键帧（与步骤 3 并行执行）...")
        else:
            logger.info("\n[步骤 4/5] Non video模式：跳过关键帧提取")

        # 检测语言并切分文本（按词/字数量）
        language = detect_language(consolidated_text)
        CHUNK_SIZE = 1000 if language == "Chinese" else 500
        OVERLAP = 60 if language == "Chinese" else 50

        chunks = self._split_subtitles_into_chunks(
            subtitle_data, CHUNK_SIZE, OVERLAP)
        chunk_texts = [chunk['text'] for chunk in chunks]

        logger.info(f"文本已切分为 {len(chunks)} 个片段")
        for idx, chunk in enumerate(chunks, start=1):
            word_count = self._count_words(chunk['text'])
            logger.info(f"  - 片段 {idx}/{len(chunks)} 词数: {word_count}")

        chunk_frames: Dict[int, List[str]] = {}
        if self.text_only:
            summary_path = self._generate_summary_with_chunks(
                temp_text_file, chunk_texts, video_title)
        else:
            frames_dir = os.path.join(
                self.output_dir, f"{video_title}_frames")
            os.makedirs(frames_dir, exist_ok=True)

            with ThreadPoolExecutor(max_workers=2) as executor:
                summary_future = executor.submit(
                    self._generate_summary_with_chunks,
                    temp_text_file, chunk_texts, video_title
                )
                frames_future = executor.submit(
                    self._extract_frames_for_chunks,
                    video_path, chunks, frames_dir,
                    frame_extraction_interval, skip_similar_frames
                )
                summary_path = summary_future.result()
                chunk_frames = frames_future.result()

        # 5. 生成最终markdown
        logger.info("\n[步骤 5/5] 生成最终markdown文档...")
        # 获取原始文件名（用于输出文件名和标题）
        original_filename = None
        if video_path:
            original_filename = os.path.splitext(
                os.path.basename(video_path))[0]
        elif subtitle_path:
            original_filename = os.path.splitext(
                os.path.basename(subtitle_path))[0]
        else:
            original_filename = video_title

        final_md_path = self._generate_final_markdown(
            summary_path, chunk_texts, chunk_frames, video_title, video_path, original_filename
        )

        # 清理中间产物
        self._cleanup_temp_files([temp_text_file, summary_path])

        logger.info("=" * 60)
        logger.info("✅ 处理完成！")
        logger.info(f"📄 最终文档: {final_md_path}")
        logger.info("=" * 60)

        return final_md_path

    @staticmethod
    def _cleanup_temp_files(paths: List[str]) -> None:
        """
        删除生成过程中的临时文件，忽略不存在的路径
        """
        for path in paths:
            if not path:
                continue
            try:
                if os.path.exists(path):
                    os.remove(path)
                    logger.info(f"🧹 已清理临时文件: {path}")
            except Exception as exc:
                logger.warning(f"无法删除临时文件 {path}: {exc}")

    def _split_subtitles_into_chunks(self, subtitle_data: SubtitleData,
                                     chunk_size: int, overlap: int) -> List[Dict[str, Any]]:
        """
        基于字幕数据按词数切分，并返回每段的文本和时间范围
        """
        if not subtitle_data:
            return []

        token_pattern = re.compile(r'[\u4e00-\u9fff]|[a-zA-Z0-9]+')
        entry_token_counts = []
        for entry in subtitle_data:
            tokens = token_pattern.findall(entry['text'])
            entry_token_counts.append(max(1, len(tokens)))

        cumulative = [0]
        for count in entry_token_counts:
            cumulative.append(cumulative[-1] + count)

        chunks = []
        start_idx = 0
        total_entries = len(subtitle_data)

        while start_idx < total_entries:
            start_tokens = cumulative[start_idx]
            target_tokens = start_tokens + chunk_size
            end_idx = bisect_left(cumulative, target_tokens, lo=start_idx + 1)
            if end_idx <= start_idx:
                end_idx = start_idx + 1
            if end_idx > total_entries:
                end_idx = total_entries

            chunk_entries = subtitle_data[start_idx:end_idx]
            chunk_text_parts = [
                entry['text'].strip() for entry in chunk_entries if entry['text'].strip()]
            chunk_text = "\n".join(chunk_text_parts).strip()

            start_time = TimeRangeExtractor.time_str_to_seconds(
                chunk_entries[0]['start'])
            end_time = TimeRangeExtractor.time_str_to_seconds(
                chunk_entries[-1]['end'])

            chunks.append({
                'text': chunk_text,
                'start_time': start_time,
                'end_time': end_time,
                'start_index': start_idx,
                'end_index': end_idx
            })

            if end_idx >= total_entries:
                break

            next_tokens = max(0, cumulative[end_idx] - overlap)
            start_idx = bisect_left(cumulative, next_tokens)
            if start_idx >= total_entries:
                break
            # 确保至少向前推进
            if start_idx == end_idx:
                start_idx += 1

        return chunks

    def _generate_summary_with_chunks(self, text_file: str, chunks: List[str],
                                      video_title: str) -> str:
        """
        生成总结并返回总结文件路径
        这里需要调用Summary.py的功能，但需要获取每个chunk的总结
        """
        # 生成每个chunk的总结
        summaries = []
        total_chunks = len(chunks)

        if self.test_mode:
            logger.info("🔧 测试模式开启：不会调用LLM，直接输出Prompt内容")
            for i, chunk in enumerate(chunks):
                current_idx = i + 1
                prompt = BASE_SYSTEM_PROMPT.format(
                    current=current_idx, total=total_chunks) + "\n\n" + chunk
                summaries.append(prompt)
        else:
            # 检查API KEY
            api_key = os.environ.get("GEMINI_API_KEY", GEMINI_API_KEY)
            if not api_key or "YOUR_API_KEY" in api_key:
                raise ValueError("GEMINI_API_KEY 未设置，请配置环境变量或在代码中填写。")
            if genai is None:
                raise ImportError(
                    "未找到 google-genai，请先安装: pip install google-genai")

            try:
                client = genai.Client(api_key=api_key, http_options={
                    'api_version': 'v1alpha'})
            except Exception as e:
                logger.error(f"初始化API客户端失败: {e}")
                raise

            for i, chunk in enumerate(chunks):
                current_idx = i + 1

                logger.info(f"  总结片段 {current_idx}/{total_chunks}...")

                max_retries = 5
                for attempt in range(1, max_retries + 1):
                    try:
                        summary = generate_chunk_summary(
                            client, chunk, current_idx, total_chunks, PRIMARY_MODEL
                        )
                        if summary and summary.strip():
                            summaries.append(summary)
                            break
                        else:
                            raise RuntimeError("LLM 返回空总结")
                    except Exception as e:
                        if attempt < max_retries:
                            logger.warning(
                                f"  片段 {current_idx} 第 {attempt}/{max_retries} 次总结失败，将在 30 秒后重试: {e}")
                            time.sleep(30)
                        else:
                            logger.error(
                                f"  片段 {current_idx} 连续 {max_retries} 次总结失败，终止当前文件处理: {e}")
                            # 直接抛出异常，中止当前文件的后续处理
                            raise RuntimeError(
                                f"片段 {current_idx} 总结在重试 {max_retries} 次后仍失败，终止本文件处理") from e

        # 保存总结到文件
        summary_path = os.path.join(
            self.output_dir, f"{video_title}_summary_temp.md")
        with open(summary_path, 'w', encoding='utf-8') as f:
            f.write(f"> 由 AI 生成，共 {len(chunks)} 部分\n\n")

            for i, summary in enumerate(summaries):
                f.write(f"\n## 第 {i+1} 部分\n\n")
                f.write(summary)
                f.write("\n\n---\n")

        return summary_path

    def _extract_frames_for_chunks(self, video_path: str,
                                   chunks: List[Dict[str, Any]],
                                   frames_dir: str,
                                   frame_extraction_interval: float,
                                   skip_similar_frames: bool) -> Dict[int, List[str]]:
        """
        为每个片段提取帧，返回片段索引到帧路径列表的映射
        """
        chunk_frames: Dict[int, List[str]] = {}

        for i, chunk in enumerate(chunks):
            start_time = chunk['start_time']
            end_time = chunk['end_time']
            time_str = f"{int(start_time//60):02d}m{int(start_time % 60):02d}s-{int(end_time//60):02d}m{int(end_time % 60):02d}s"
            chunk_dir_name = sanitize_filename(
                f"chunk_{i+1:02d}_{time_str}")
            chunk_frames_dir = os.path.join(frames_dir, chunk_dir_name)

            # 测试模式下如果目录已存在则直接复用，避免重新提取
            if self.test_mode and os.path.isdir(chunk_frames_dir):
                logger.info(
                    f"  片段 {i+1}/{len(chunks)}: {time_str} -> 使用现有帧目录，跳过提取")
                existing_files = [
                    os.path.join(chunk_frames_dir, f)
                    for f in sorted(os.listdir(chunk_frames_dir))
                    if f.lower().endswith(('.jpg', '.jpeg', '.png', '.bmp'))
                ]
                deduped = self._deduplicate_frame_paths(existing_files)
                chunk_frames[i] = deduped
                logger.info(
                    f"    复用 {len(deduped)} 帧（去重前 {len(existing_files)}）")
                continue

            logger.info(f"  片段 {i+1}/{len(chunks)}: {time_str} -> 提取帧...")
            frame_files = self.time_extractor.extract_frames_in_range(
                video_path, start_time, end_time,
                chunk_frames_dir,
                interval=frame_extraction_interval,
                skip_similar=skip_similar_frames
            )
            deduped_files = self._deduplicate_frame_paths(frame_files)
            chunk_frames[i] = deduped_files
            if len(deduped_files) != len(frame_files):
                logger.info(
                    f"    提取 {len(frame_files)} 帧，去重后保留 {len(deduped_files)}")
            else:
                logger.info(f"    提取了 {len(frame_files)} 帧")

        return chunk_frames

    def _generate_final_markdown(self, summary_path: str, chunks: List[str],
                                 chunk_frames: Dict[int, List[str]],
                                 video_title: str, video_path: str, original_filename: str) -> str:
        """
        生成最终的markdown文档，包含总结和截图
        
        Args:
            summary_path: 总结临时文件路径
            chunks: 片段文本列表
            chunk_frames: 片段索引到帧路径列表的映射
            video_title: 处理后的视频标题（用于内部标识）
            video_path: 视频文件路径（可能为None）
            original_filename: 原始文件名（用于输出文件名和一级标题）
        """
        # 读取总结内容
        with open(summary_path, 'r', encoding='utf-8') as f:
            summary_content = f.read()

        # 使用原始文件名作为输出文件名
        final_md_path = os.path.join(
            self.output_dir, f"{original_filename}.md")

        with open(final_md_path, 'w', encoding='utf-8') as f:
            # 写入一级标题（原始文件名）
            f.write(f"# {original_filename}\n\n")

            # 解析总结，找到每个部分
            # 使用更灵活的方式分割内容
            parts = re.split(r'\n## 第 (\d+) 部分\n', summary_content)

            # 如果分割成功，parts应该是: [标题和开头内容, '1', 第一部分内容, '2', 第二部分内容, ...]
            # parts[0] 包含 "> 由 AI 生成，共 X 部分\n\n"，我们直接跳过它
            if len(parts) > 1:

                # 处理每个部分
                for i in range(1, len(parts), 2):
                    if i + 1 >= len(parts):
                        break

                    part_num = parts[i]
                    part_content = parts[i + 1]

                    try:
                        chunk_idx = int(part_num) - 1
                    except ValueError:
                        continue

                    # 写入部分标题
                    f.write(f"\n## 第 {part_num} 部分\n\n")

                    frames_for_chunk = chunk_frames.get(chunk_idx, [])

                    # 再写总结内容（去除末尾的---分隔符）
                    part_content_clean = part_content.rstrip(
                        '\n').rstrip('---').rstrip('\n').strip()

                    sections = [
                        s.strip() for s in re.split(
                            r'\n\s*---+\s*\n', part_content_clean)
                        if s.strip()
                    ]

                    inserted_by_section = False
                    if frames_for_chunk and len(sections) > 1:
                        allocations = self._allocate_frame_counts(
                            sections, len(frames_for_chunk))
                        frame_cursor = 0
                        for section_idx, section_text in enumerate(sections):
                            num_frames = allocations[section_idx] if section_idx < len(
                                allocations) else 0
                            if num_frames > 0:
                                section_frames = frames_for_chunk[
                                    frame_cursor:frame_cursor + num_frames]
                                self._write_frame_block(
                                    f, section_frames, final_md_path)
                                frame_cursor += num_frames
                            f.write(section_text)
                            f.write("\n\n")
                            if section_idx < len(sections) - 1:
                                f.write("---\n\n")
                        inserted_by_section = True

                    if not inserted_by_section:
                        if frames_for_chunk:
                            self._write_frame_block(
                                f, frames_for_chunk, final_md_path)
                        f.write(part_content_clean)

                    f.write("\n\n---\n\n")
            else:
                # 如果无法分割，直接写入整个内容
                f.write(summary_content)

        return final_md_path

    def _write_frame_block(self, file_obj: TextIO, frame_paths: List[str],
                           final_md_path: str) -> None:
        """
        将一组帧以 Markdown 图片形式写入
        """
        if not frame_paths:
            return

        # file_obj.write("### 📸 相关截图\n\n")
        base_dir = os.path.dirname(final_md_path)
        for frame_path in frame_paths:
            if not os.path.exists(frame_path):
                continue
            try:
                rel_path = os.path.relpath(frame_path, base_dir)
                rel_path = self._format_md_path(rel_path)
                file_obj.write(f"![截图]({rel_path})\n\n")
            except ValueError:
                fallback_path = self._format_md_path(frame_path)
                file_obj.write(f"![截图]({fallback_path})\n\n")

    def _deduplicate_frame_paths(self, frame_paths: List[str],
                                 similarity_threshold: float = 0.97) -> List[str]:
        """
        对图片路径按内容相似度去重
        """
        if not frame_paths:
            return frame_paths

        deduped: List[str] = []
        reference_images: List[np.ndarray] = []
        for path in frame_paths:
            if not os.path.exists(path):
                continue
            image = cv2.imread(path)
            if image is None:
                continue
            is_duplicate = False
            for ref_img in reference_images:
                similarity = TimeRangeExtractor._calculate_similarity(
                    ref_img, image)
                if similarity >= similarity_threshold:
                    is_duplicate = True
                    break

            if is_duplicate:
                continue

            deduped.append(path)
            reference_images.append(image)

        if len(deduped) != len(frame_paths):
            logger.info(
                f"    去重后保留 {len(deduped)}/{len(frame_paths)} 帧")
        return deduped

    def _allocate_frame_counts(self, sections: List[str],
                               total_frames: int) -> List[int]:
        """
        根据每个段落的字数按比例分配截图数量
        """
        if total_frames <= 0:
            return [0] * len(sections)

        weights: List[int] = []
        for section in sections:
            weight = self._count_words(section)
            weights.append(weight if weight > 0 else 1)

        total_weight = sum(weights)
        if total_weight == 0:
            total_weight = len(sections)
            weights = [1] * len(sections)

        allocations: List[int] = []
        remainders: List[float] = []
        assigned = 0
        for weight in weights:
            exact = (total_frames * weight) / total_weight
            alloc = int(exact)
            allocations.append(alloc)
            remainders.append(exact - alloc)
            assigned += alloc

        remaining = total_frames - assigned
        if remaining > 0:
            order = sorted(
                range(len(sections)),
                key=lambda idx: remainders[idx],
                reverse=True
            )
            idx = 0
            while remaining > 0 and order:
                target = order[idx % len(order)]
                allocations[target] += 1
                remaining -= 1
                idx += 1

        return allocations

    @staticmethod
    def _count_words(text: str) -> int:
        """
        统计中英文词数：中文逐字计数，英文按连续字母数字计数
        """
        tokens = re.findall(r'[\u4e00-\u9fff]|[a-zA-Z0-9]+', text)
        return len(tokens)

    @staticmethod
    def _format_md_path(path: str) -> str:
        """
        将文件路径规范化为 Markdown 可用的 URL，处理空格等特殊字符
        """
        normalized = path.replace(os.sep, '/')
        return quote(normalized, safe="/:-_.()")


def main():
    """主函数"""
    import argparse

    parser = argparse.ArgumentParser(
        description='视频总结应用 - 下载视频、生成AI总结并提取关键帧',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  python video_summary_app.py "https://www.youtube.com/watch?v=xxx"
  python video_summary_app.py "https://www.bilibili.com/video/xxx" -o my_output
  python video_summary_app.py "https://www.bilibili.com/video/xxx" -c cookies.txt
  python video_summary_app.py "https://youtube.com/watch?v=xxx" -i 3.0
  
参数说明:
  -o: 输出目录（默认: output）
  -i: 帧提取间隔（秒），默认: 2.0
  -c: Cookies 文件路径（用于 Bilibili 等需要登录的网站）
        """
    )

    parser.add_argument('url', nargs='?', default=None,
                        help='视频链接（YouTube/Bilibili等），可选（本地模式可省略）')
    parser.add_argument('-o', '--output', default='output',
                        help='输出目录，默认: output')
    parser.add_argument('-i', '--interval', type=float, default=2.0,
                        help='帧提取间隔（秒），默认: 2.0')
    parser.add_argument(
        '-t', '--test', action='store_true',
        help='测试模式：不调用LLM，直接把Prompt写入输出，便于查看上下文')
    parser.add_argument(
        '-n', '--text-only', action='store_true',
        help='Non video模式：不下载视频、不提取截图，仅输出文本总结')
    parser.add_argument(
        '--local-video', type=str, default=None,
        help='本地视频文件路径（配合本地字幕或仅提取帧）')
    parser.add_argument(
        '--local-subtitle', type=str, default=None,
        help='本地字幕文件路径（SRT）；text-only 模式下只需该参数')
    parser.add_argument(
        '--title', type=str, default=None,
        help='手动指定输出标题（可选）')
    parser.add_argument(
        '-c', '--cookies', type=str, default=None,
        help='Cookies 文件路径（用于 Bilibili 等需要登录的网站），例如: --cookies cookies.txt')

    args = parser.parse_args()

    try:
        # 验证 cookies 文件是否存在
        cookies_file = args.cookies
        if cookies_file and not os.path.exists(cookies_file):
            logger.warning(f"警告: Cookies 文件不存在: {cookies_file}，将不使用 cookies")
            cookies_file = None
        elif cookies_file:
            logger.info(f"✅ 使用 Cookies 文件: {cookies_file}")

        if not args.url and not args.local_subtitle:
            parser.error("必须提供视频链接或本地字幕文件")
        if not args.text_only and not (args.url or args.local_video):
            parser.error("非 text-only 模式需要视频链接或本地视频文件")

        app = VideoSummaryApp(output_dir=args.output,
                              test_mode=args.test,
                              text_only=args.text_only,
                              cookies_file=cookies_file)
        result_path = app.process_video(
            args.url,
            frame_extraction_interval=args.interval,
            local_video=args.local_video,
            local_subtitle=args.local_subtitle,
            provided_title=args.title)
        if result_path:
            print(f"\n✅ 完成！结果文件: {result_path}")
        else:
            logger.info("本次任务未生成输出文件。")
    except Exception as e:
        logger.error(f"处理失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
