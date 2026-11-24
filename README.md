# Spark Audio Skills

把长视频和播客整理成带摘要的高光切片，把新闻文稿制作成双人语音节目。两个可供 Agent 调用的本地 Skill，接入讯飞录音文件转写大模型与超拟人语音合成，使用 FFmpeg 完成媒体处理。

[快速开始](docs/quickstart.md) · [真实案例与试听](docs/case-studies.md) · [验证记录](docs/validation.md) · [参与贡献](CONTRIBUTING.md)

## 两个 Skill

| Skill | 适用场景 | 输入 | 输出 |
| --- | --- | --- | --- |
| [语音高光切片](skills/xfyun-highlight-clips/SKILL.md) | 从 B 站视频、访谈、长播客提取适合分发的内容 | 本地视频或音频，也可导入已有时间轴转写 | 识别稿、SRT、高光摘要、MP4／MP3 切片、Markdown 清单 |
| [双音色播客](skills/xfyun-dual-voice-podcast/SKILL.md) | 将新闻、人物故事、行业评论改写为双人节目 | 双角色对谈 JSON | MP3／WAV、段级字幕、角色文稿、可复用语音片段 |

Agent 负责理解材料、筛选高光、写摘要与组织对话；脚本负责转码、接口请求、状态保存和音频拼接。项目不内置文本大模型，成品保存在本地。

## 先看结果

已用一段 **13 分 06 秒**的 B 站视频完成转写，得到 **75 个句段**，裁出 **68.80 秒、50.10 秒、58.85 秒**三个高光并配上摘要。[查看原片来源与高光位置](docs/case-studies.md#长视频高光切片)。

同一段新闻文稿的四音色试听，以及一集 **125.15 秒**的娱乐化双人新闻播客，均为实际 API 合成结果。下列音频为 **AI 合成声音**。

| 示例 | 时长 | 文件 |
| --- | --- | --- |
| 聆飞逸同文试听 | 18.44 秒 | [MP3](examples/media/voice-lingfeiyi.mp3) |
| 聆玉言同文试听 | 18.12 秒 | [MP3](examples/media/voice-lingyuyan.mp3) |
| 聆小璇同文试听 | 19.12 秒 | [MP3](examples/media/voice-lingxiaoxuan.mp3) |
| 聆小玥同文试听 | 20.08 秒 | [MP3](examples/media/voice-lingxiaoyue.mp3) |
| 黄仁勋相关新闻双人对谈 | 125.15 秒 | [MP3](examples/media/jensen-news-comedy.mp3) · [文稿](examples/jensen-news-comedy.json) |

试听参数、处理过程及验证范围见[案例文档](docs/case-studies.md)。原始 B 站视频和剪辑素材不随仓库分发。

## 快速开始

需要 **Python 3.10+、FFmpeg 与 ffprobe**，FFmpeg 应包含 `libx264` 和 `libmp3lame`。以下为 macOS／Linux shell 命令。

```bash
git clone https://github.com/inhai-wiki/spark-audio-skills.git
cd spark-audio-skills
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/offline_demo.py --out artifacts/offline-demo
```

离线样例会生成测试视频和测试音，跑通裁切与拼接，**API 请求数为 0**。它用于检查安装和处理流程，真实声音效果请听上方 MP3。输出目录需为新建或空目录。

需要真实转写与合成时，复制凭证模板并在本地填写，再检查依赖。

```bash
cp -n .env.example .env
chmod 600 .env
.venv/bin/python scripts/run.py highlight doctor
.venv/bin/python scripts/run.py podcast doctor
```

`doctor` 不发网络请求。完整配置与故障处理见[快速开始](docs/quickstart.md)。

## 安装给 Agent

```bash
.venv/bin/python scripts/install.py
```

默认安装到 `CODEX_HOME/skills`；未设置 `CODEX_HOME` 时使用 `~/.codex/skills`。安装器保留已有同名目录，遇到冲突会停止。其他支持此 Skill 格式的 Agent，可通过 `--dest ./agent-skills` 指定发现目录。安装后按宿主要求刷新技能。

复制后的独立 Skill 从进程环境读取凭证。若继续使用项目根目录的 `.env`，让 Agent 通过本项目虚拟环境和 `scripts/run.py` 执行。两种方式见[Agent 配置](docs/quickstart.md#配置-agent)。

## 从长视频提取高光

准备已取得的本地素材，预处理会返回后续使用的 `job` 目录。

```bash
.venv/bin/python scripts/run.py highlight prepare inputs/interview.mp4 --out outputs/highlights

# 将此值替换为 prepare 返回的 job 目录。
JOB="outputs/highlights/你的任务目录"
.venv/bin/python scripts/run.py highlight transcribe-file --job "$JOB"
```

`transcribe-file` 会上传音频并消耗对应服务额度。让 Agent 读取任务里的 `transcript.md`，按[清单格式](skills/xfyun-highlight-clips/references/manifests.md)写入 `highlights.json`，包含每段的起止时间、标题、摘要及选择理由，再执行：

```bash
.venv/bin/python scripts/run.py highlight clip --job "$JOB" --manifest "$JOB/highlights.json"
.venv/bin/python scripts/run.py highlight clip --job "$JOB" --manifest "$JOB/highlights.json" --audio-only
```

输出位于任务的 `clips/` 目录，可同时保留视频与音频版本。已有时间轴时，可用 `import-transcript` 完全离线处理。完整流程见[高光操作指南](docs/quickstart.md#从本地视频提取高光)。

## 把新闻文稿做成双人播客

先按[脚本格式](skills/xfyun-dual-voice-podcast/references/script-format.md)准备两位角色，让主持人交代事件，另一位补充评论和追问。两个角色各固定一个已授权音色。

```bash
.venv/bin/python scripts/run.py podcast validate examples/jensen-news-comedy.json
.venv/bin/python scripts/run.py podcast plan examples/jensen-news-comedy.json --out outputs/news-plan.json

# render 会调用 TTS 并消耗对应服务额度。
.venv/bin/python scripts/run.py podcast render examples/jensen-news-comedy.json --out outputs/news
```

`plan` 只统计请求与字符，不提交合成。成品为 `outputs/news/podcast.mp3`。修改部分对话后，在同一目录加 `--resume`，可复用未变的成功片段。持续制作多期节目时，每期使用独立输出目录。

需要选声音时，可先用附带的同文试听工具：

```bash
.venv/bin/python scripts/voice_audition.py examples/news-voice-auditions-four.json --out outputs/auditions
```

此命令会提交四次 TTS 请求，按音色顺序执行。它属于项目辅助工具，单独安装 Skill 时不会复制。恢复方式、离线拼接与产物说明见[播客操作指南](docs/quickstart.md#生成双音色新闻播客)。

## 凭证与费用

| 服务 | 本地环境变量 |
| --- | --- |
| 录音文件转写大模型 | `XFYUN_APP_ID`、`XFYUN_API_KEY`、`XFYUN_API_SECRET` |
| 超拟人语音合成 | `XFYUN_TTS_APP_ID`、`XFYUN_TTS_API_PASSWORD` |

凭证保存在本地 `.env` 或进程环境中，不写入文稿和命令参数。服务权限、音色授权、免费额度与价格以讯飞账户控制台为准。ASR、TTS 和 Agent 使用的文本模型分别计费；字符统计不代表账单金额。

成功请求可从同一输出目录恢复。未知请求结果会停止并保留状态，程序不会自动重复提交可能计费的请求。详细说明见[恢复与费用](docs/quickstart.md#恢复任务与费用)。

## 使用边界

- 接受本地媒体文件，不内置 B 站或播客平台下载器。
- 文件转写使用上传后异步查询流程；可选听写 V2.0 后端按短音频分块处理。两者的协议、权限与时间精度不同。
- ASR 提供识别稿与时间信息，高光选择和摘要由 Agent 完成。发布前需校对专有名词、数字和片段接缝。
- 播客要求两个不同音色轮流发言，字幕为段级时间轴；未实现多人同时发声或逐字对齐。
- 当前任务按顺序执行，未提供生产级并发调度器。测试与文件解码结果不代表识别准确率基准或主观听感验收。

接口细节见[文件转写](skills/xfyun-highlight-clips/references/file-asr.md)、[可选听写后端](skills/xfyun-highlight-clips/references/protocol.md)和 [TTS 接口说明](skills/xfyun-dual-voice-podcast/references/api-notes.md)。

## 参与贡献

欢迎改进媒体兼容性、任务恢复、示例与文档。开发依赖及离线测试：

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

贡献流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。缺陷反馈可提交到 [Issues](https://github.com/inhai-wiki/spark-audio-skills/issues)，请使用脱敏日志和可复现样例。

## 许可证

代码采用 [MIT License](LICENSE)。第三方 API、音色和媒体素材的使用条件由其提供方规定，项目许可证不授予这些第三方内容的权利。
