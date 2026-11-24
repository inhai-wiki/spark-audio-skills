---
name: xfyun-dual-voice-podcast
description: 将新闻、人物故事、股票分析或已有对谈文稿编排为双角色播客，调用讯飞超拟人语音合成 API，按固定音色逐段生成并拼接 MP3、WAV 和字幕。用于新闻音频、双人配音和可改单段的播客制作。
---

# 讯飞双音色播客

把来源材料交给 Agent 编排为双角色脚本，再用本技能的确定性脚本完成语音请求、缓存和拼接。脚本不调用文本模型，不会自行联网查找材料或生成对话。

新闻传播场景可采用主持人与评论者的分工。主持人提出新闻事件里的问题，评论者依据原始报道解释时间线、公告内容和市场反应。用户已有完整对谈稿时，保留其观点和角色轮次。股票分析中的日期、金额及盘中／收盘口径需要先核对，不让语音脚本补写行情或因果关系。

本 Skill 的 `examples/podcast.json` 提供四轮财经对谈格式。完整代码项目根目录还提供 `examples/jensen-news-podcast.json`，围绕黄仁勋回归与英伟达股价反弹安排六轮对话。该新闻脚本有明确历史日期，再次使用时先核对时效。

## 使用前

- Python 3.10+、`websocket-client`、FFmpeg、ffprobe。
- 在线合成需要 `XFYUN_TTS_APP_ID` 与 `XFYUN_TTS_API_PASSWORD` 环境变量。使用超拟人语音合成控制台的 APIPassword，区别于 ASR 的 APIKey。不要把密钥写进脚本 JSON、终端参数或稿件。
- 发音人需在账号中单独开通权限。示例的两个音色只是官方列表中的候选，不表示当前账号已获授权。
- 本技能独立安装后以技能目录为工作目录运行 `python scripts/podcast.py ...`。项目根入口可能代为加载 `.env`，本脚本自身仅读取已设置的环境变量。

先运行 `python scripts/podcast.py doctor`。它只报告配置是否存在，不展示值、不发网络请求。

## 编排与执行

1. 阅读用户指定的新闻、人物故事或对谈稿，确定听众、主题、两位角色的分工和目标篇幅。保留来源事实，清楚区分观点、假设和引用。未完成的事实核查交给 Agent 处理，语音脚本不补写新闻数据。
2. 使用 [脚本格式](references/script-format.md) 写 JSON。两个角色固定使用不同且已授权的 `voice`，每轮只含实际需要朗读的纯文本。问答有承接关系，避免把角色标签、舞台提示或 Markdown 送入 TTS。角色需要换音色时按用户偏好修改映射。
3. 验证并生成请求计划，查看段数和字符量。计划只估算待提交字符数，实际费用以账号套餐和账单为准。
4. 用户请求制作播客所授权的合成范围内，使用 `render` 生成。若语音调用因额度、授权或网络问题失败，保留状态并报告真实原因，避免重复计费。
5. 播放导出的 MP3，检查两位角色、专名、数字、停顿和接缝。SRT 是按实际音频段计时的字幕，段内没有字词级对齐。按平台需要加 AI 合成声音标识，不冒充真人录音。

```bash
python scripts/podcast.py validate examples/podcast.json
python scripts/podcast.py plan examples/podcast.json --out /absolute/path/requests.json
python scripts/podcast.py render examples/podcast.json --out /absolute/path/podcast-output
```

`render` 逐段发送完整文本，接收流式音频。长轮次会按 UTF-8 字节预算分段，保持文本原样，优先在句末拆分。分段可能影响连贯性，建议 Agent 主动把每轮写成适合口播的短段。

批量生成多期新闻音频时，为不同文稿准备独立 JSON 和输出目录，保留同一套角色与音色映射，再按篇顺序调用。遇到失败或未知结果停止当前批次，先处理记录。不要把同一篇稿件换到新目录重复提交。

## 先做同文试听

音色可以先用同一段新闻文案试听，再固定到两个角色。当前项目已成功合成聆飞逸、聆玉言、聆小璇、聆小玥四个同文样本，语速、音量与音高均为 50。它们的听感由读者对照判断，不把同参数下的时长差异写成质量排名。

完整代码项目另有 `scripts/voice_audition.py` 和 `examples/news-voice-auditions-four.json`，在项目根目录运行以下命令。它们属于项目辅助文件，单独复制本 Skill 时不包含这两个根目录文件。

```bash
.venv/bin/python scripts/voice_audition.py examples/news-voice-auditions-four.json --out outputs/voice-auditions
.venv/bin/python scripts/voice_audition.py examples/news-voice-auditions-four.json --out outputs/voice-auditions --resume
```

辅助工具每种音色单独生成 MP3／WAV，并把状态写入 `audition-manifest.json`。它复用现有渲染器，串行执行，失败即停，绝不自动重试未知结果。输出均标注为 AI 合成声音。`x6_lingyuzhao_pro` 在本次返回 `10163` 参数拒绝，不能写成成功试听音色。

## 修改与恢复

改一段脚本后，在同一输出目录运行 `render ... --resume`，未变的内容和音色配置命中缓存，已成功段落不再提交。不要为同一任务反复换输出目录，否则无法复用缓存。

连接中断、进程中止或发出请求后报错，状态会保留为 `inflight` 或 `uncertain`。这时服务端可能已经处理并计费。先查看服务控制台，明确需要重新提交时再使用 `--resume --retry-uncertain`，不要自动替用户追加该参数。它允许重试未知结果请求，可能再次计费。单次执行没有自动网络重试。

同一输出目录在 render 期间持有 `.render.lock` 排他锁，其他进程会停止。正常退出会释放锁，崩溃后残留的锁不会自动删除。先根据锁中的 PID 核查进程与服务控制台，再手动移除确认已失效的锁。

服务报错时，状态文件保留错误码和脱敏信息。握手失败仅记录 HTTP 状态码，网络异常不保存原始请求头。

缓存丢失或损坏时会停止，防止悄悄重新生成。可以恢复文件，或明确决定再次生成后另选输出目录。

## 离线拼接

已有分角色音频时，在每条 `turn` 加入 `audio_path`。相对路径以 JSON 所在目录解析。

```bash
python scripts/podcast.py assemble /absolute/path/script-with-audio.json --out /absolute/path/offline-output
```

此命令不请求讯飞、不验证音色效果。它将本地音频统一成 24 kHz 单声道 WAV，加上轮次停顿，再做整体响度处理和 MP3 导出。测试音、其他服务音频及真人录音均保留其原有来源身份，不将离线合成结果称为讯飞音色实测。已有输出需明确加 `--overwrite`，并与在线 render 输出分开保存。

## 交付

返回 `podcast.mp3`、`podcast.wav`、`podcast.srt`、`script.md`，以及两位角色的独立文本。保留 `script.json`、`requests.json`、`timeline.json`、`provenance.json`，在线模式额外保留 `state.json` 和 `segments/`，用于核查、改单段和恢复。

双人角色选择和脚本编排属于 Agent，语音合成属于讯飞 API，停顿、响度和拼接属于本地处理。完整节目采用顺序轮流发言，不生成多人同时说话。

协议、音色候选与限制见 [接口说明](references/api-notes.md)。
