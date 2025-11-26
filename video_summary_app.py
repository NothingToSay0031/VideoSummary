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
from datetime import datetime
from bisect import bisect_left
from urllib.parse import quote
from typing import List, Dict, Any, Tuple
from concurrent.futures import ThreadPoolExecutor
import cv2
import numpy as np

try:
    from google import genai
except ImportError:
    genai = None

# ==== LLM 配置（可根据需要修改）====
# os.environ.get("GEMINI_API_KEY", "")
GEMINI_API_KEY = ""
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


def parse_subtitles(file_content: str) -> Tuple[SubtitleData, str]:
    """
    解析 SRT/VTT 字幕，返回结构化字幕列表与整合文本
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

    def download(self, url: str) -> Dict[str, str]:
        """
        下载视频和字幕

        Args:
            url: 视频链接（YouTube/Bilibili等）

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

        try:
            info_output = subprocess.run(
                info_cmd, capture_output=True, text=True, check=True
            )
            video_info = json.loads(info_output.stdout)
            video_title = video_info.get('title', 'video')
            logger.info(f"📹 检测到视频标题: {video_title}")
            # 清理标题中的非法字符
            video_title = re.sub(r'[<>:"/\\|?*]', '_', video_title)
            logger.info(f"📝 清理后的标题: {video_title}")
        except Exception as e:
            logger.warning(f"获取视频信息失败: {e}，使用默认标题")
            video_title = 'video'

        # 检查本地是否已有视频和字幕文件
        logger.info("检查本地是否已有视频和字幕文件...")
        existing_video_path = None
        existing_subtitle_path = None

        # 查找本地视频文件（匹配标题）
        video_extensions = ['.mp4', '.mkv', '.webm', '.flv', '.avi']
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
                            potential_path = os.path.join(self.output_dir, f)
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

            # 查找英文或中文字幕（优先中文）
            # 检查自动生成的字幕（通常用en,zh等简写）
            # 检查手动字幕（通常用en-US,zh-CN等）
            # 检查Bilibili AI字幕（ai-zh, ai-en等）
            # yt-dlp --list-subs 输出格式：Language    Formats (如 "ai-zh    srt")
            if re.search(r'\bai-zh\b', available_subs, re.IGNORECASE):
                subtitle_lang = 'ai-zh'
                logger.info("找到简体中文字幕 ai-zh（优先使用）")
            elif re.search(r'\b(zh-cn|zh_CN|chinese)\b', available_subs, re.IGNORECASE):
                subtitle_lang = 'zh-cn'
                logger.info("找到简体中文字幕（优先使用）")
            elif re.search(r'\b(zh-tw|zh_TW)\b', available_subs, re.IGNORECASE):
                subtitle_lang = 'zh-tw'
                logger.info("找到繁体中文字幕（优先使用）")
            elif re.search(r'\bzh\b', available_subs, re.IGNORECASE):
                subtitle_lang = 'zh'
                logger.info("找到中文字幕（优先使用）")
            elif re.search(r'\bai-en\b', available_subs, re.IGNORECASE):
                subtitle_lang = 'ai-en'
                logger.info("找到英文字幕 ai-en")
            elif re.search(r'\b(en|english)\b', available_subs, re.IGNORECASE):
                subtitle_lang = 'en'
                logger.info("找到英文字幕")
            else:
                logger.warning("未找到中文或英文字幕，将尝试下载所有可用字幕")
                subtitle_lang = 'all'  # 下载所有字幕，后续选择
        except Exception as e:
            logger.warning(f"检查字幕失败: {e}，将尝试下载所有字幕")
            subtitle_lang = 'all'

        # 如果已有本地视频，跳过下载
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
                                similarity_threshold: float = 0.95) -> List[str]:
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
        last_frame = None

        # 跳转到开始位置
        cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

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
                is_similar = False
                if skip_similar and last_frame is not None:
                    similarity = TimeRangeExtractor._calculate_similarity(
                        last_frame, frame)
                    if similarity >= similarity_threshold:
                        is_similar = True
                        skipped_similar += 1

                if not is_similar:
                    time_str = TimeRangeExtractor.seconds_to_time_str(
                        current_time)
                    filename = f"frame_{extracted_count:03d}_{time_str.replace(':', '')}.{ext}"
                    filepath = os.path.join(output_dir, filename)

                    if encode_param:
                        cv2.imwrite(filepath, frame, encode_param)
                    else:
                        cv2.imwrite(filepath, frame)

                    extracted_files.append(filepath)
                    extracted_count += 1
                    if skip_similar:
                        last_frame = frame.copy()

            frame_count += 1

        cap.release()

        if skip_similar and skipped_similar > 0:
            logger.info(f"    跳过相似帧: {skipped_similar}")

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


class VideoSummaryApp:
    """视频总结应用主类"""

    def __init__(self, output_dir: str = "output", test_mode: bool = False, cookies_file: str = None):
        """
        初始化应用

        Args:
            output_dir: 输出目录
            test_mode: 是否启用测试模式（不调用LLM，仅输出Prompt）
            cookies_file: Cookies 文件路径（用于 Bilibili 等需要登录的网站）
        """
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)
        self.downloader = VideoDownloader(
            os.path.join(output_dir, "downloads"), cookies_file=cookies_file)
        self.time_extractor = TimeRangeExtractor()
        self.test_mode = test_mode

    def process_video(self, url: str,
                      frame_extraction_interval: float = 2.0,
                      skip_similar_frames: bool = True) -> str:
        """
        处理视频：下载、解析、总结、提取帧、生成markdown

        Args:
            url: 视频链接
            frame_extraction_interval: 帧提取间隔（秒）
            skip_similar_frames: 是否跳过相似帧

        Returns:
            生成的markdown文件路径
        """
        logger.info("=" * 60)
        logger.info("开始处理视频")
        logger.info("=" * 60)

        # 1. 下载视频和字幕
        logger.info("\n[步骤 1/5] 下载视频和字幕...")
        download_result = self.downloader.download(url)
        video_path = download_result['video']
        subtitle_path = download_result['subtitle']
        video_title = download_result['title']

        if not subtitle_path:
            logger.error("未找到字幕文件，无法继续处理")
            raise ValueError("需要字幕文件才能生成总结")

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
        logger.info("\n[步骤 4/5] 提取关键帧（与步骤 3 并行执行）...")

        # 检测语言并切分文本（按词/字数量）
        language = detect_language(consolidated_text)
        CHUNK_SIZE = 2000 if language == "Chinese" else 1700
        OVERLAP = 150 if language == "Chinese" else 120

        chunks = self._split_subtitles_into_chunks(
            subtitle_data, CHUNK_SIZE, OVERLAP)
        chunk_texts = [chunk['text'] for chunk in chunks]

        logger.info(f"文本已切分为 {len(chunks)} 个片段")
        for idx, chunk in enumerate(chunks, start=1):
            word_count = self._count_words(chunk['text'])
            logger.info(f"  - 片段 {idx}/{len(chunks)} 词数: {word_count}")

        frames_dir = os.path.join(self.output_dir, f"{video_title}_frames")
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
        final_md_path = self._generate_final_markdown(
            summary_path, chunk_texts, chunk_frames, video_title, video_path
        )

        logger.info("=" * 60)
        logger.info("✅ 处理完成！")
        logger.info(f"📄 最终文档: {final_md_path}")
        logger.info("=" * 60)

        return final_md_path

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
                try:
                    summary = generate_chunk_summary(
                        client, chunk, current_idx, total_chunks, PRIMARY_MODEL
                    )
                    if summary:
                        summaries.append(summary)
                    else:
                        summaries.append(f"\n> [错误: 第 {current_idx} 部分总结为空]\n")
                except Exception as e:
                    logger.warning(f"  片段 {current_idx} 总结失败: {e}")
                    summaries.append(
                        f"\n> [错误: 第 {current_idx} 部分总结失败: {str(e)}]\n")

        # 保存总结到文件
        summary_path = os.path.join(
            self.output_dir, f"{video_title}_summary_temp.md")
        with open(summary_path, 'w', encoding='utf-8') as f:
            f.write(f"# {video_title} 学习笔记\n\n")
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
            chunk_frames_dir = os.path.join(
                frames_dir, f"chunk_{i+1:02d}_{time_str}")

            # 测试模式下如果目录已存在则直接复用，避免重新提取
            if self.test_mode and os.path.isdir(chunk_frames_dir):
                logger.info(
                    f"  片段 {i+1}/{len(chunks)}: {time_str} -> 使用现有帧目录，跳过提取")
                existing_files = [
                    os.path.join(chunk_frames_dir, f)
                    for f in sorted(os.listdir(chunk_frames_dir))
                    if f.lower().endswith(('.jpg', '.jpeg', '.png', '.bmp'))
                ]
                chunk_frames[i] = existing_files
                logger.info(f"    复用 {len(existing_files)} 帧")
                continue

            logger.info(f"  片段 {i+1}/{len(chunks)}: {time_str} -> 提取帧...")
            frame_files = self.time_extractor.extract_frames_in_range(
                video_path, start_time, end_time,
                chunk_frames_dir,
                interval=frame_extraction_interval,
                skip_similar=skip_similar_frames
            )
            chunk_frames[i] = frame_files
            logger.info(f"    提取了 {len(frame_files)} 帧")

        return chunk_frames

    def _generate_final_markdown(self, summary_path: str, chunks: List[str],
                                 chunk_frames: Dict[int, List[str]],
                                 video_title: str, video_path: str) -> str:
        """
        生成最终的markdown文档，包含总结和截图
        """
        # 读取总结内容
        with open(summary_path, 'r', encoding='utf-8') as f:
            summary_content = f.read()

        final_md_path = os.path.join(self.output_dir, f"{video_title}_最终总结.md")

        with open(final_md_path, 'w', encoding='utf-8') as f:
            # 写入文件头
            f.write(f"# {video_title} 视频总结\n\n")
            f.write(
                f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            f.write(f"> 源视频: {os.path.basename(video_path)}\n\n")
            f.write("---\n\n")

            # 解析总结，找到每个部分
            # 使用更灵活的方式分割内容
            parts = re.split(r'\n## 第 (\d+) 部分\n', summary_content)

            # 如果分割成功，parts应该是: [标题和开头内容, '1', 第一部分内容, '2', 第二部分内容, ...]
            if len(parts) > 1:
                # 写入开头内容（如果有）
                if parts[0].strip():
                    # 跳过文件头（# 标题 和 > 注释）
                    header_end = parts[0].find('\n---\n')
                    if header_end > 0:
                        parts[0] = parts[0][header_end + 5:]
                    if parts[0].strip():
                        f.write(parts[0].strip())
                        f.write("\n\n---\n\n")

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

                    # 先展示截图
                    if chunk_idx in chunk_frames and chunk_frames[chunk_idx]:
                        f.write("### 📸 相关截图\n\n")
                        for frame_path in chunk_frames[chunk_idx]:
                            if os.path.exists(frame_path):
                                try:
                                    rel_path = os.path.relpath(
                                        frame_path, os.path.dirname(final_md_path))
                                    rel_path = self._format_md_path(rel_path)
                                    f.write(f"![截图]({rel_path})\n\n")
                                except ValueError:
                                    fallback_path = self._format_md_path(
                                        frame_path)
                                    f.write(
                                        f"![截图]({fallback_path})\n\n")

                    # 再写总结内容（去除末尾的---分隔符）
                    part_content_clean = part_content.rstrip(
                        '\n').rstrip('---').rstrip('\n').strip()
                    f.write(part_content_clean)
                    f.write("\n\n---\n\n")
            else:
                # 如果无法分割，直接写入整个内容
                f.write(summary_content)

        return final_md_path

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

    parser.add_argument('url', help='视频链接（YouTube/Bilibili等）')
    parser.add_argument('-o', '--output', default='output',
                        help='输出目录，默认: output')
    parser.add_argument('-i', '--interval', type=float, default=2.0,
                        help='帧提取间隔（秒），默认: 2.0')
    parser.add_argument(
        '-t', '--test', action='store_true',
        help='测试模式：不调用LLM，直接把Prompt写入输出，便于查看上下文')
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

        app = VideoSummaryApp(output_dir=args.output,
                              test_mode=args.test, cookies_file=cookies_file)
        result_path = app.process_video(
            args.url, frame_extraction_interval=args.interval)
        print(f"\n✅ 完成！结果文件: {result_path}")
    except Exception as e:
        logger.error(f"处理失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
