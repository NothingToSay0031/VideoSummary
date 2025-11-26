# 视频总结应用（Video Summary App）

一个一站式的长视频学习助手：输入 YouTube / Bilibili 等平台的链接，程序会自动完成视频/字幕下载、字幕分段、Gemini 总结、关键帧抓取，并输出带截图的 Markdown 学习笔记。支持自定义输出目录、帧提取参数、测试模式、Cookies 认证等多种配置，方便迁移到本地或自动化流水线。

---

## 功能亮点

1. **智能下载**  
   使用 `yt-dlp` 自动选择最优画质、优先抓取中文字幕（回退英文），同时支持复用本地已下载资源以节省时间。

2. **字幕与文本处理**  
   兼容 SRT/VTT，自动清理 BOM、序号，生成结构化字幕和纯文本稿。

3. **LLM 总结**  
   基于 Google Gemini（默认 `gemini-2.5-pro`），按语言自适应切块，输出分部分的结构化总结；支持 `--test` 模式快速定位提示词。

4. **关键帧提取**  
   将每段总结映射回视频时间戳，按自定义间隔批量抓帧，跳过相似帧以减少冗余。

5. **Markdown 输出**  
   自动生成 `视频标题_最终总结.md`，图文并茂，使用相对路径方便分享/同步。

---

## 目录结构

```
VideoSummary/
├── video_summary_app.py   # 主入口 + 任务编排
├── extract_frames.py      # 帧提取工具
├── srt.py                 # 字幕解析工具
├── Summary.py             # 旧版总结逻辑（可参考）
├── requirements.txt       # 依赖
└── output/                # 运行后生成的结果目录
```

---

## 环境要求

- Python 3.9+
- FFmpeg（yt-dlp 会自动调用，系统需可执行）
- `pip install -r requirements.txt`
- 可用的 Google Gemini API Key（Pro 级模型可免费试用，注意配额）

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
```

### 常用参数

| 参数 | 说明 | 默认值 |
| --- | --- | --- |
| `url` | 必填，支持 YouTube / Bilibili / 任何 yt-dlp 支持的平台 | - |
| `-o / --output` | 输出根目录，内部会自动创建 `downloads/`、`_frames/` 等 | `output` |
| `-i / --interval` | 帧提取间隔（秒），越小截图越密集 | `2.0` |
| `-c / --cookies` | Cookies 文件路径，为登录受限视频提供权限 | `None` |
| `-t / --test` | 测试模式：不调用 LLM，只输出 Prompt，便于调参数 | `False` |

示例：

```bash
# 默认配置
python video_summary_app.py "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

# 指定输出目录 + 自定义帧间隔
python video_summary_app.py "https://www.bilibili.com/video/BVxxxx" -o bili_output -i 3.0

# 传入 Cookies 并启用测试模式
python video_summary_app.py "https://www.bilibili.com/video/BVxxxx" -c cookies.txt -t
```

---

## Cookies 说明

1. **为什么需要 Cookies？**  
   - 访问会员/付费/已登录用户才能看的视频。  
   - 读取 Bilibili AI 字幕（例如 `ai-zh`）通常需要登录。

2. **如何导出？**  
   - 推荐安装浏览器插件（如 Get cookies.txt）。  
   - 登录目标网站后，通过插件导出 Netscape 格式文件。

3. **如何使用？**  
   - 运行时追加 `-c 路径`。程序会在构建 `yt-dlp` 命令时自动注入 `--cookies <file>`。  
   - 若文件不存在会有警告并回退为无 Cookie 模式。

---

## 处理流程

1. **视频与字幕获取**  
   `VideoDownloader` 通过 `yt-dlp` 先拉取视频信息、字幕列表，优先命中 `ai-zh > zh-CN > zh > ai-en > en`，若本地已有文件会自动复用。

2. **解析与分段**  
   解析字幕，`parse_subtitles` 会输出带时间戳的结构体与纯文本稿。`detect_language` 决定切片大小（中文 2k token / 英文 1.7k token）。

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
- **测试模式**：`-t / --test` 会直接把 Prompt 写进总结文件，用于检查上下文是否正确，特别适合调试提示词或 chunk 大小。

---

## 常见问题

1. **没有字幕怎么办？**  
   目前必须依赖字幕；可先用 Youtube/Bilibili AI 字幕或第三方工具生成字幕后放到 `downloads/` 并重命名匹配标题。

2. **截图和内容不匹配？**  
   文本到时间段的映射基于字幕时间戳，若字幕与画面不同步可适当增大 `interval`、修改 `TextToTimeMapper` 或手动挑选关键帧。

3. **Gemini 报错 / 速率限制？**  
   检查 `GEMINI_API_KEY` 是否正确，或在 `.env` / 环境变量里配置多个 Key。必要时可以切换为较低延迟模型。

4. **大视频耗时太久？**  
   - 先用 `yt-dlp` 下载到本地，程序会复用。  
   - 降低帧提取频率（`-i`），或只保留部分 chunk。  
   - 如果只是想看总结，可用 `-t` 跳过帧提取逻辑。

---

## 许可证

MIT License。欢迎 Fork、二次开发或嵌入自己的生产流程，记得保护 API Key 和 Cookies 安全。欢迎提 Issue/PR 交流。

