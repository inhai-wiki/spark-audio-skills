# Spark Audio Skills

面向内容团队的两个语音 Skill：从长视频和播客中提取带摘要的高光切片，把新闻与行业文稿制作成双人语音节目。接入讯飞录音文件转写大模型与超拟人语音合成，由 Agent 编排内容，FFmpeg 完成媒体处理。

[快速开始](docs/quickstart.md) · [案例与试听](docs/case-studies.md) · [参与贡献](CONTRIBUTING.md)

## 两个内容场景

| Skill | 团队工作场景 | 输入 | 交付 |
| --- | --- | --- | --- |
| [语音高光切片](skills/xfyun-highlight-clips/SKILL.md) | 把访谈、技术分享、B 站视频或长播客拆成适合分发的内容 | 本地视频、音频或已有时间轴转写 | 识别稿、字幕、高光摘要、MP4／MP3 切片、Markdown 清单 |
| [双音色播客](skills/xfyun-dual-voice-podcast/SKILL.md) | 将新闻、人物故事和行业评论持续制作成双人节目 | 双角色对谈文稿 | MP3／WAV、段级字幕、角色文稿、可复用语音片段 |

高光流程把片段、摘要和原音位置放在一起，编辑可以先判断选题，再回到原素材核对。播客流程固定角色与音色，让团队沿用同一套节目设置处理下一篇文稿；局部改稿时，可以复用未变的成功片段。

Agent 负责选高光、写摘要和组织对话，脚本负责转码、语音接口调用及音频拼接。成品和任务记录保存在本地。

## 实际结果

一条约 13 分钟的 B 站视频，整理出「工具调用循环」「上下文压缩」「Harness 工程」三个高光，配有摘要和选择理由。[查看来源与片段位置](docs/case-studies.md#长视频高光切片)。

新闻播客示例使用聆飞逸与聆玉言，把一篇新闻材料改成约两分钟的双人对谈。四份同文试听方便比较声音，选择栏目的角色搭配。以下音频均为 **AI 合成声音**。

| 示例 | 试听 |
| --- | --- |
| 聆飞逸 | [MP3](examples/media/voice-lingfeiyi.mp3) |
| 聆玉言 | [MP3](examples/media/voice-lingyuyan.mp3) |
| 聆小璇 | [MP3](examples/media/voice-lingxiaoxuan.mp3) |
| 聆小玥 | [MP3](examples/media/voice-lingxiaoyue.mp3) |
| 黄仁勋相关新闻双人对谈 | [MP3](examples/media/jensen-news-comedy.mp3) · [文稿](examples/jensen-news-comedy.json) |

新闻示例保留具体日期与来源，复用时请更新内容。原始 B 站视频与剪辑素材不随仓库分发。

## 快速开始

需要 **Python 3.10+、FFmpeg 与 ffprobe**，FFmpeg 包含 `libx264` 和 `libmp3lame`。以下命令适用于 macOS／Linux shell。

```bash
git clone https://github.com/inhai-wiki/spark-audio-skills.git
cd spark-audio-skills
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp -n .env.example .env
chmod 600 .env
```

在本地编辑 `.env`，填写已开通服务的凭证：

| 服务 | 环境变量 |
| --- | --- |
| 录音文件转写大模型 | `XFYUN_APP_ID`、`XFYUN_API_KEY`、`XFYUN_API_SECRET` |
| 超拟人语音合成 | `XFYUN_TTS_APP_ID`、`XFYUN_TTS_API_PASSWORD` |

安装依赖、离线演示、完整命令与故障处理见[快速开始](docs/quickstart.md)。凭证保存在本地，不写进文稿或提交到仓库。

## 安装给 Agent

```bash
.venv/bin/python scripts/install.py
```

默认安装到 `CODEX_HOME/skills`，未设置时使用 `~/.codex/skills`。已有同名目录会保留。其他支持此 Skill 格式的 Agent，可通过 `--dest ./agent-skills` 指定发现目录，安装后按宿主要求刷新技能。

独立安装的 Skill 从进程环境读取凭证。继续使用项目根目录的 `.env` 时，让 Agent 在本项目中通过虚拟环境和 `scripts/run.py` 执行。配置方式见[Agent 指南](docs/quickstart.md#配置-agent)。

## 让 Agent 处理一份长视频

准备本地媒体后，可以把下面的任务交给 Agent，替换其中的文件路径：

> 使用 xfyun-highlight-clips 处理 inputs/interview.mp4。选出三个适合独立传播的完整观点，每段给出标题、摘要和选择理由，同时导出视频切片、音频切片、完整识别稿与字幕。结果保存到 outputs/highlights，保留原音时间位置，方便后续核对和分发。

也可以先在命令行预处理素材，再让 Agent 根据转写完成选段：

```bash
.venv/bin/python scripts/run.py highlight prepare inputs/interview.mp4 --out outputs/highlights
```

预处理返回每份素材的任务目录，后续依次完成转写、编写高光清单与裁切。操作步骤见[高光指南](docs/quickstart.md#从本地视频提取高光)。

## 让 Agent 制作一期双人节目

把已经核对的新闻或行业文稿交给 Agent，沿用固定角色与音色：

> 使用 xfyun-dual-voice-podcast，把 inputs/news.md 改成适合收听的双人短节目。主持人用聆飞逸交代事件和提问，搭档用聆玉言补充评论。保留事实来源与日期，采用短轮次对话，输出到 outputs/news，交付 MP3、字幕和角色文稿。

已有对谈 JSON 时，可直接合成。此命令会调用 TTS 并使用对应服务额度：

```bash
.venv/bin/python scripts/run.py podcast render examples/jensen-news-comedy.json --out outputs/news
```

修改部分对话后，在同一输出目录加 `--resume` 可复用未变片段。音色选择、同文试听与文稿格式见[播客指南](docs/quickstart.md#生成双音色新闻播客)。

## 使用与费用

项目接受本地媒体文件，高光选择和摘要由 Agent 完成。播客使用两个不同音色轮流发言，字幕按段生成。多份素材或多期节目当前按顺序执行，每项保留独立输出目录。

ASR 与 TTS 分别开通服务、配置凭证和计费，音色需具备对应授权；Agent 使用的文本模型成本另计。额度和价格以讯飞控制台为准。任务保留执行状态与缓存，恢复方式见[任务恢复说明](docs/quickstart.md#恢复任务与费用)。

## 贡献与许可证

欢迎改进媒体兼容性、任务恢复、示例与文档。开发依赖在 `requirements-dev.txt`，协作流程见 [CONTRIBUTING.md](CONTRIBUTING.md)，问题反馈请提交到 [Issues](https://github.com/inhai-wiki/spark-audio-skills/issues)。

代码采用 [MIT License](LICENSE)。第三方 API、音色和媒体素材遵循各自的使用条件。
