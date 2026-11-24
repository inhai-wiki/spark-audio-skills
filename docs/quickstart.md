# 快速开始

本指南从项目根目录运行命令。需要 Python 3.10+、FFmpeg 和 ffprobe，FFmpeg 包含 `libx264` 与 `libmp3lame`。命令按 macOS／Linux shell 编写；当前验证环境见[验证记录](validation.md)。

Python 需另行安装到满足上述版本要求。安装 FFmpeg 时，按当前系统选择一条命令：

```bash
# macOS，已安装 Homebrew。
brew install ffmpeg

# Ubuntu。
sudo apt-get install ffmpeg
```

## 安装与离线检查

```bash
git clone https://github.com/inhai-wiki/spark-audio-skills.git
cd spark-audio-skills
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/offline_demo.py --out artifacts/offline-demo
```

离线样例生成测试视频、手工时间轴和测试音，完成两种格式的高光裁切与双角色音频拼接。`artifacts/offline-demo/verification.json` 记录处理结果，`api_requests` 应为 `0`。再次运行时换一个新目录。真实声音示例见[案例文档](case-studies.md)。

## 配置凭证

```bash
cp -n .env.example .env
chmod 600 .env
```

在本地编辑 `.env`，填入对应服务控制台的值。

| 变量 | 用途 |
| --- | --- |
| `XFYUN_APP_ID` | 录音文件转写所属应用 |
| `XFYUN_API_KEY` | 录音文件转写 API Key |
| `XFYUN_API_SECRET` | 录音文件转写 API Secret |
| `XFYUN_TTS_APP_ID` | 超拟人语音合成所属应用 |
| `XFYUN_TTS_API_PASSWORD` | 超拟人语音合成 APIPassword |

ASR 与 TTS 需要分别开通服务，使用的 TTS 音色也需获得授权。保留 `.env.example` 的空模板即可，不把真实凭证提交到仓库。项目的 `.gitignore` 已排除 `.env`。

`scripts/run.py` 只读取允许的配置字段，不执行 shell 表达式；已有进程环境变量优先于 `.env`。凭证检查只报告是否存在：

```bash
.venv/bin/python scripts/run.py highlight doctor
.venv/bin/python scripts/run.py podcast doctor
```

上述检查不发网络请求，不能据此确认账户权限或剩余额度。

## 配置 Agent

两个 Skill 的入口分别为：

- [xfyun-highlight-clips/SKILL.md](../skills/xfyun-highlight-clips/SKILL.md)
- [xfyun-dual-voice-podcast/SKILL.md](../skills/xfyun-dual-voice-podcast/SKILL.md)

可以让 Agent 在项目中读取这些文件，也可以安装到其技能发现目录：

```bash
.venv/bin/python scripts/install.py
```

默认目标是 `CODEX_HOME/skills`，未设置 `CODEX_HOME` 时使用 `~/.codex/skills`。如果任一同名目录已经存在，安装器会停止。指定其他兼容目录：

```bash
.venv/bin/python scripts/install.py --dest ./agent-skills
```

每个 Skill 自带脚本、参考说明、示例和运行依赖声明。独立脚本从进程环境读取凭证；项目根目录的 `.env` 只由 `scripts/run.py` 和同文试听辅助工具加载。使用项目配置时，可告诉 Agent：

> 在此仓库根目录工作，使用 `.venv/bin/python scripts/run.py` 调用 Skill。凭证由本地配置加载，不把内容写入输出。先给出请求计划，再按当前任务执行。

安装器不会安装 Python 依赖，也不会复制根目录的 `voice_audition.py`。技能刷新方式以 Agent 宿主为准。

## 从本地视频提取高光

场景：拿到一集播客或一个 B 站视频的本地文件，希望提取几个完整观点，得到可分发切片、对应摘要和原始识别稿。项目不包含平台下载步骤；将已取得且可使用的素材放到 `inputs/` 等本地目录。

预处理支持一次输入多个文件，依次为每个素材创建独立任务：

```bash
.venv/bin/python scripts/run.py highlight prepare inputs/interview.mp4 inputs/podcast.mp3 --out outputs/highlights
```

每个返回结果包含 `job` 路径。选定当前任务后，设置变量用于后续命令：

```bash
# 替换为 prepare 实际返回的目录。
JOB="outputs/highlights/你的任务目录"
```

预处理会提取 16 kHz 单声道 MP3，并生成兼容听写后端使用的 PCM 分块。推荐的录音文件转写后端上传整段 MP3，再查询结果：

```bash
.venv/bin/python scripts/run.py highlight transcribe-file --job "$JOB"
```

这一步会向讯飞上传音频，并使用已开通的服务额度。默认关闭文本顺滑和口语规整，便于保留原始识别结果。接口支持的单文件上限为 5 小时、500 MB，规格与参数见[文件转写说明](../skills/xfyun-highlight-clips/references/file-asr.md)。

返回任务仍在处理时，保留当前目录，用 `--resume` 恢复查询：

```bash
.venv/bin/python scripts/run.py highlight transcribe-file --job "$JOB" --resume
```

`--resume` 只查询已保存的订单，不重新上传；没有订单号时会拒绝执行。轮询达到上限仍未完成时，命令可能以退出码 `2` 返回待处理状态，应查看 JSON 结果后恢复查询。

已有经过对齐的转写时，跳过云端识别，导入时间轴：

```bash
.venv/bin/python scripts/run.py highlight import-transcript --job "$JOB" --input inputs/interview-transcript.json
```

让 Agent 读取任务中的 `transcript.md`，并提出具体要求：

> 从这期内容里选三个适合独立传播的完整观点。每段保留必要上下文，写出标题、一至三句摘要、选择理由和对应句段 ID。时间以当前转写为准，将清单保存为任务目录中的 `highlights.json`。

清单结构见[高光格式说明](../skills/xfyun-highlight-clips/references/manifests.md)。`summary` 由 Agent 根据原文编写；脚本保存摘要并导出可读清单。确定片段后裁切：

```bash
.venv/bin/python scripts/run.py highlight clip --job "$JOB" --manifest "$JOB/highlights.json"
.venv/bin/python scripts/run.py highlight clip --job "$JOB" --manifest "$JOB/highlights.json" --audio-only
```

| 产物 | 用途 |
| --- | --- |
| `transcript.json`、`transcript.md`、`transcript.srt` | 识别稿及时间轴，作为逐字校对的起点 |
| `clips/` 中的 MP4／MP3 | 实际裁出的媒体片段 |
| `clips/highlights-video.md`、`clips/highlights-audio.md` | 各片段的标题、摘要、时间、选择理由及媒体链接 |
| `clips/clips-video.json`、`clips/clips-audio.json` | 对应模式最近一次完整结果 |
| `clips/clips.json` | 最近一次裁切状态，读取前检查是否为 `complete` |

识别稿与摘要分开保存。专有名词、否定词、数字和切口仍需回听；缺少完整词时间时，摘录可能包含相交句段的上下文。视频音轨偏移超过 50 ms 时，预处理会要求先对齐素材。

可选短音频后端使用 `highlight transcribe "$JOB"`，依次提交 PCM 分块，默认每块 20 秒、最大可配置 55 秒。它需要单独开通听写 V2.0，输出时间精度为分块级，不能直接替代文件转写的词句时间。详见[可选后端协议](../skills/xfyun-highlight-clips/references/protocol.md)。

## 生成双音色新闻播客

场景：把已核对的新闻、人物故事或行业评论做成双人短节目。Agent 负责改写，主持人交代事件并发问，另一位角色给出补充、评论和回应。固定两位角色和音色后，可以持续制作同一栏目的不同选题。

先复制示例修改内容，或直接检查仓库中的新闻对谈稿：

```bash
.venv/bin/python scripts/run.py podcast validate examples/jensen-news-comedy.json
.venv/bin/python scripts/run.py podcast plan examples/jensen-news-comedy.json --out outputs/news-plan.json
```

`validate` 检查两位角色、不同音色、轮次与文本字段；`plan` 输出字符数量、请求分段及角色配置，不请求 TTS，也不报价。JSON 写法见[脚本格式](../skills/xfyun-dual-voice-podcast/references/script-format.md)。

核对文稿和账户可用音色后执行合成：

```bash
.venv/bin/python scripts/run.py podcast render examples/jensen-news-comedy.json --out outputs/news
```

脚本按轮次请求语音，统一为 24 kHz 单声道音频，添加停顿、处理整体响度并导出成品。

| 产物 | 用途 |
| --- | --- |
| `podcast.mp3`、`podcast.wav` | 完整节目 |
| `podcast.srt` | 按合成片段长度排布的段级字幕 |
| `script.json`、`script.md` | 角色配置与文稿 |
| `timeline.json` | 角色、文本与片段时间信息 |
| `requests.json`、`state.json`、`segments/` | 请求计划、执行状态与成功音频缓存 |

修改其中一轮内容后，在原输出目录恢复：

```bash
.venv/bin/python scripts/run.py podcast render examples/jensen-news-comedy.json --out outputs/news --resume
```

未变的成功片段会复用；更改文字或声音参数会生成新的请求。仅改停顿时，可以复用声音并重新拼接。不同节目使用独立输出目录，按篇顺序生成。

已有各轮音频时，在脚本的每个轮次加入 `audio_path`，可离线拼接：

```bash
.venv/bin/python scripts/run.py podcast assemble inputs/script-with-audio.json --out outputs/offline-podcast
```

音频路径相对于脚本 JSON 所在目录解析。新闻示例带有具体日期，复用前应更新来源和数据；语音脚本不负责事实核查。字幕目前没有字词级对齐，成品需要试听角色、数字、专有名词与接缝。

## 同文多音色试听

同一段文稿、相同语速等参数，可以帮助选择栏目的角色声音。四音色配置见 [news-voice-auditions-four.json](../examples/news-voice-auditions-four.json)。

```bash
.venv/bin/python scripts/voice_audition.py examples/news-voice-auditions-four.json --out outputs/auditions
.venv/bin/python scripts/voice_audition.py examples/news-voice-auditions-four.json --out outputs/auditions --resume
```

第一条命令会按顺序请求 TTS。每种音色保存于 `outputs/auditions/<id>/`，包含 `podcast.mp3`、`podcast.wav` 及请求状态。根目录的 `audition-manifest.json` 记录各音色结果与实际时长。第二条命令用于恢复，复用已成功片段。

此辅助工具复用播客脚本的请求与缓存实现，以单音色独立输出试听，不改变播客 Skill 的双角色约束。失败即停止；未知结果不会自动重试。

## 恢复任务与费用

| 情况 | 处理方式 |
| --- | --- |
| 文件转写已有订单但未完成 | 同一个 `job` 加 `--resume` 查询 |
| TTS 中断后已有成功片段 | 同一文稿与输出目录加 `--resume` |
| 修改部分对话 | 原目录 `--resume`，复用未变的成功片段 |
| 返回音色或权限错误 | 核对控制台开通情况及文档中的 `vcn`，修正后再执行 |
| 返回未知请求状态 | 先检查服务控制台与本地状态，不删除状态文件来绕过保护 |

主 Skill 提供显式 `--retry-uncertain` 选项；它可能再次计费，只有确认重新提交时才使用。TTS 的该选项需同时提供 `--resume`。同文试听工具不暴露这一选项。

ASR、TTS 的额度和价格以各自控制台为准，两个服务的套餐不能混算。`plan` 和试听清单里的字符数只记录输入规模，不代表实际扣费；Agent 文本模型的使用成本单独计算。不要为复用成功请求随意更换输出目录或丢弃缓存。

## 开发与排查

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q scripts skills tests
```

更多开发约定见 [CONTRIBUTING.md](../CONTRIBUTING.md)。反馈问题时提供命令、环境、错误码和脱敏后的状态，不附真实凭证或私人音频。

官方参考：[录音文件转写大模型](https://www.xfyun.cn/doc/spark/asr_llm/Ifasr_llm.html)、[语音听写 V2.0](https://www.xfyun.cn/doc/spark/spark_zh_iat_new.html)、[超拟人语音合成](https://www.xfyun.cn/doc/spark/super%20smart-tts.html)。
