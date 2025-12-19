# 视频总结应用（Video Summary App）

一个一站式的长视频学习助手：输入 YouTube / Bilibili 等平台的链接，程序会自动完成视频/字幕下载、字幕分段、Gemini 总结、关键帧抓取，并输出带截图的 Markdown 学习笔记。支持自定义输出目录、帧提取参数、测试模式、Cookies 认证等多种配置，方便迁移到本地或自动化流水线。

目前功能仍然非常简陋，仅有我个人使用且未经大量测试，欢迎大家交流想法！欢迎Fork，欢迎二次开发。

[AI自动生成笔记的B站/YouTube视频总结小工具 | 个人玩具分享](https://zhuanlan.zhihu.com/p/1977102486092416383)

---

## 重要! 

使用前请定制Prompt！目前的Prompt是针对图形学/游戏引擎的内容，可以按需进行修改。

## 环境要求

- Python
- `pip install -r requirements.txt`
- 可用的 Google Gemini API Key

---

## 安装与基础配置

```bash
git clone <repo-url>
cd VideoSummary
python -m venv .venv && source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 配置 Gemini API Key

支持两种方式：

1. **环境变量（推荐）**
   ```powershell
   # PowerShell
   $Env:GEMINI_API_KEY = "your_key_here"
   ```
   ```bash
   # Bash
   export GEMINI_API_KEY="your_key_here"
   ```
2. **代码里写死**  
   修改 `video_summary_app.py` 顶部的 `GEMINI_API_KEY` 字段（不推荐在公共仓库使用）。

### 准备 Cookies（可选）

部分 Bilibili 字幕需要登录态。可以用浏览器扩展导出 Cookie 文件（如 `cookies.txt`），然后参考 yt-dlp 官方说明：<https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp>

> 导出完成后，将文件放到项目根目录，并通过 `-c cookies.txt` 传入即可；程序在执行前会校验文件是否存在。


### 修改Prompt（可选）

Prompt写在 `video_summary_app.py` 里，变量名为 `BASE_SYSTEM_PROMPT`。里面的Prompt目前是非常定制化针对图形学/游戏引擎的内容，可以按需进行修改。

---

## 运行方式

```bash
python video_summary_app.py "<视频链接>" [选项]
python video_summary_app.py --local-video "<本地视频文件路径>" --local-subtitle "<本地字幕文件路径>" --title "<输出标题>"
```

### 常用参数

| 参数 | 说明 | 默认值 |
| --- | --- | --- |
| `url` | 支持 YouTube / Bilibili / 任意 yt-dlp 平台；走本地流程时可留空 | `None` |
| `-o / --output` | 输出根目录，内部会自动创建 `downloads/`、`_frames/` 等 | `output` |
| `-i / --interval` | 帧提取间隔（秒），越小截图越密集 | `2.0` |
| `-c / --cookies` | Cookies 文件路径，为登录受限视频提供权限；程序会先校验文件存在 | `None` |
| `-t / --test` | 测试模式：不调用 LLM，只输出 Prompt，便于调试上下文 | `False` |
| `-n / --text-only` | 仅生成文字总结：跳过视频下载与帧提取 | `False` |
| `--local-video` | 本地视频文件路径：配合 `--local-subtitle` | `None` |
| `--local-subtitle` | 本地字幕（SRT）；text-only 模式只需字幕即可运行 | `None` |
| `--title` | 手动指定输出 Markdown 标题，覆盖自动推断 | `None` |

> 使用约束：  
> - 必须至少提供「视频链接」或「本地字幕」其一。  
> - 在非 `--text-only` 模式下，还需要提供「视频链接」或「本地视频」用于提帧。

示例：

```bash
# 默认配置
python video_summary_app.py "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

# 指定输出目录 + 自定义帧间隔
python video_summary_app.py "https://www.bilibili.com/video/BVxxxx" -o bili_output -i 3.0

# 传入 Cookies 并启用测试模式
python video_summary_app.py "https://www.bilibili.com/video/BVxxxx" -c cookies.txt -t

# 仅生成文字总结，跳过视频与帧处理
python video_summary_app.py "https://www.youtube.com/watch?v=dQw4w9WgXcQ" -n -c "D:\Downloads\ytb_cookie.txt"
```
---

## 处理流程

1. **视频与字幕获取**  
   `VideoDownloader` 通过 `yt-dlp` 先拉取视频信息、字幕列表，若本地已有文件会自动复用。

2. **解析与分段**  
   解析字幕，`parse_subtitles` 会输出带时间戳的结构体与纯文本稿。`detect_language` 决定切片大小（中文2k词 / 英文 1.7k词 长度约为10min视频量），可按需调整。

3. **生成总结 & 抓帧（并行）**  
   - `ThreadPoolExecutor` 开两个任务：  
     - `_generate_summary_with_chunks`：按片段调用 Gemini，总结写入 `{title}_summary_temp.md`。  
     - `_extract_frames_for_chunks`：调用 `TimeRangeExtractor` 在对应时间段抓帧，目录格式 `chunk_01_00m00s-05m30s/`。

4. **合成最终 Markdown**  
   `_generate_final_markdown` 将总结内容与截图拼接为 `{title}_最终总结.md`，每部分都会先展示截图再展示文字，路径自动转换为相对地址。

---

## 输出结构

```
output/
├── downloads/
│   ├── <title>.mp4 / .mkv / ...
│   └── <title>.zh.vtt / <title>.en.srt
├── <title>_transcript.txt       # 纯文本稿
├── <title>_summary_temp.md      # 中间总结
├── <title>_frames/
│   └── chunk_01_00m00s-05m30s/
│       ├── frame_000_000000.jpg
│       └── ...
└── <title>_最终总结.md           # 最终交付文档
```

---

## 高级配置

- **提示词**：修改 `BASE_SYSTEM_PROMPT` 可切换总结语气/结构（注意保持 `${current}` / `${total}` 占位符）。  
- **模型 / API 版本**：调整 `PRIMARY_MODEL` 或创建不同的 `genai.Client` 配置。  
- **文本切片**：`CHUNK_SIZE`、`OVERLAP` 定义在 `process_video` 内，可针对不同语言/视频类型调整。  
- **帧提取策略**：`TimeRangeExtractor.extract_frames_in_range` 支持 `skip_similar`、图片格式、质量等参数；如需更细粒度可改写函数。  
- **测试模式**：`-t / --test` 会直接把 Prompt 写进总结文件，用于检查上下文是否正确，适合调试提示词或 chunk 大小。

---

## 常见问题

1. **没有字幕怎么办？**  
   目前必须依赖字幕；可先用 Youtube/Bilibili AI 字幕或第三方工具（如通义听悟，Whisper等）生成字幕后使用本地字幕参数指定本地字幕文件路径。

2. **截图和内容不匹配？**  
   文本到时间段的映射基于字幕时间戳，若字幕与画面不同步可适当增大 `interval`、修改 `TextToTimeMapper` 或手动挑选关键帧。

3. **Gemini 报错 / 速率限制？**  
   检查 `GEMINI_API_KEY` 是否正确，或在 `.env` / 环境变量里配置多个 Key。必要时可以切换为较低延迟模型。

4. **大视频耗时太久？**  
   - 使用其他下载器把视频下载到本地，然后使用 `--local-video` 参数指定本地视频文件路径。  
   - 降低帧提取频率（`-i`），或只保留部分 chunk。  

---

## 许可证

MIT License。欢迎 Fork、二次开发或嵌入自己的生产流程，记得保护 API Key 和 Cookies 安全。欢迎提 Issue/PR 交流。

