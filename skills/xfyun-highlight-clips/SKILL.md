---
name: xfyun-highlight-clips
description: 将本地播客音频或已取得的 B 站视频转写，辅助 Agent 挑选语义完整的高光片段、逐片撰写内容摘要，并按核实的时间裁切原素材。采用讯飞录音文件转写大模型，交付切片、摘要 Markdown、转写稿与字幕，支持订单恢复和已有时间轴导入。
---

# 讯飞语音高光切片

接收本地已取得的播客音频或 B 站视频，提取音轨并保存统一 MP3，再交付未二次润色的识别稿、SRT、高光切片及每段内容摘要。用户只给链接时先取得可用的本地文件，本技能不内置下载器。高光选择和摘要由使用本 Skill 的 Agent 依据转写完成，ASR 负责识别文字和返回时间信息。代码经过离线与协议模拟测试；录音文件后端已用 12.58 秒自制双音色语音小样完成一次上传、查询、转写与切片联调。该小样用于确认链路可用，不能代表方言、噪声、长录音等场景的识别表现。可选 V2.0 后端尚未真实调用验证。

脚本位于本技能目录内的 `scripts/highlight_clips.py`。从任意目录使用脚本的实际绝对路径运行。依赖 Python 3.10+、`ffmpeg`、`ffprobe`。推荐的录音文件后端只使用 Python 标准库；可选 V2.0 流式后端另需 `websocket-client`。独立复制安装时，运行 `python -m pip install -r requirements.txt`。

## 执行流程

1. 运行 `doctor` 检查工具和凭证是否存在，命令不会联网，也不会输出凭证内容。
2. 对一批本地素材执行 `prepare`。读取输出 JSON 里的真实 `job` 路径，后续命令使用该路径。
3. 用户已授权云端转写且本地凭证就绪时，对每个 job 执行 `transcribe-file --job JOB`。只获离线处理授权时，使用 `import-transcript` 导入用户已有的时间轴文本。
4. 阅读 `transcript.md` 和 `transcript.json`，结合受众、时长和主题挑选高光，填写 `highlights.json`。为每段写 `summary`，交代这一段讲了什么、结论及适用条件；`reason` 单独解释为什么选择它。
5. 调用 `clip` 从原始文件重编码裁切，检查结果时长并回听边界。视频输入默认输出 MP4，音频输入输出 MP3。`--audio-only` 可让视频也输出 MP3。脚本保留 Agent 写的摘要并导出 `highlights.md`，每段附带时间与切片链接。

```bash
python scripts/highlight_clips.py doctor
python scripts/highlight_clips.py prepare /absolute/bilibili-video.mp4 /absolute/podcast.mp3 --out /absolute/output
python scripts/highlight_clips.py transcribe-file --job /absolute/output/实际-job-目录
python scripts/highlight_clips.py clip --job /absolute/output/实际-job-目录 --manifest /absolute/highlights.json
```

独立安装时将示例脚本路径替换为技能脚本的绝对路径。所有示例素材与 job 路径均需替换。批量预处理生成独立 job；云端转写按 job 顺序执行，遇到未知提交或失败先处理当前订单，不继续批量提交。

## 推荐：录音文件转写大模型

环境变量为 `XFYUN_APP_ID`、`XFYUN_API_KEY`、`XFYUN_API_SECRET`，对应已开通录音文件转写大模型的应用。凭证由用户在本地配置，不进入脚本参数、仓库、文章或聊天。脚本不读取其他项目配置，不下载远程媒体。

`prepare` 从原始文件生成 16 kHz、单声道 MP3。`transcribe-file` 将该 MP3 作为二进制文件上传到 `/v2/upload`，保存 `orderId` 后通过 `/v2/getResult` 查询进度与结果。上传时长取实际 MP3 的 `ffprobe` 结果，单位为毫秒；单次上传限制为 5 小时、500 MB。此流程处理已录制文件，任务在服务端异步执行。

默认使用 `autodialect`，关闭文本顺滑和口语规整，保留服务返回的原始识别文本。可以加 `--role-type 1 --role-num 2` 请求通用角色分离；不确定人数时保持 `--role-num 0`。`--domain tech` 等领域选项按素材选择。多语种 `--language autominor` 与声纹角色分离需要相应权限。完整请求规则与选项见 [录音文件协议](references/file-asr.md)。

上传前写入状态，取得订单号后先持久化再轮询。再次运行相同参数会继续查询已有订单，完整结果会直接复用。`--max-polls 3 --poll-interval 5` 可缩短一次等待；处理未完成时返回 `pending` 和退出码 2，使用原命令加 `--resume` 继续。`--resume` 在没有订单号时会拒绝上传。

```bash
python scripts/highlight_clips.py transcribe-file --job /absolute/job --max-polls 3 --poll-interval 5
python scripts/highlight_clips.py transcribe-file --job /absolute/job --resume
```

上传连接中断或返回不明确时，默认阻止重复提交。保存的成功上传响应可用于恢复订单号。`--retry-uncertain` 明确允许再次提交，可能重复计费，只有用户授权承担此情况时才使用。不要删除状态或改变参数来绕过保护。改变语种、角色、领域等识别设置会生成新的运行目录，可能重新上传与计费；恢复时保持原设置。

文件接口提供句起止毫秒和相对句起点的词帧位置。脚本输出句级 SRT，并将词时间保留在 `transcript.json` 的 `words` 中。标点的起止可相同，角色标签由服务返回。词边界是模型估计，仍需回听；SRT 也需要根据成片节奏拆行与校正。源文件与 MP3 编码尾部存在小幅时长差时，句末最多容许 250 ms 超出并裁到源文件结尾，JSON 保留原始句起止与裁边标记。超限结果会报错，原始响应仍在。

## 可选：短音频听写 V2.0

只有需要演示短音频流式识别、且已开通相应服务时使用 `transcribe JOB...`。该后端使用独立的 WebSocket 听写接口，其协议和套餐与录音文件转写不同。

`prepare` 同时生成 16 kHz、16 bit、单声道 PCM，默认切为 20 秒分段，可用 `--chunk-seconds` 在 1 至 55 秒内调整。每段独立调用 V2.0，以 40 ms 间隔发送音频。开启 `dwa=wpgs`，按 `apd/rpl` 合并动态修正，再按源文件偏移拼接。成功片段缓存复用；未知片段默认拒绝重试。可用 `--context /absolute/context.json` 或已开通的 `--res-id` 提供上下文，细节见 [V2.0 协议](references/protocol.md)。

该后端输出 **chunk 级粗时间轴**，固定分段可能切开词句。SRT 每条对应完整音频块，适合索引；不能将它描述为逐词对齐字幕。Agent 应合并相邻块构成完整高光，裁切后回听与调整。需要更细时轴时使用推荐文件接口或导入已有准确时间文本。

## 高光选择

先读完整转写，再根据用户需求选择自成一段的观点、案例、方法或有信息量的问答。保留观点依赖的前提、限定和否定词。开头有「这个」「刚才」等指代时向前补足上下文，结尾保留解释和结论。不同候选明显重复时保留最完整的一段。

`highlights.json` 中每个片段包含真实的 `start`、`end`、`title`、`reason`、`summary` 和对应 `segment_ids`。使用服务返回的位置或回听后核实的位置，不凭语言模型估计某句话的秒数。相邻句或音频块可合并为完整高光。格式见 [manifest 说明](references/manifests.md) 和 [高光示例](examples/highlights.json)。

给每段撰写一至三句内容摘要，让读者不播放也能知道它讨论的问题和观点。摘要覆盖选定片段实际包含的内容，保留归因、前提与不确定性，不把嘉宾预测写成事实，不补充片外故事。摘要可以用于后续分发说明，但引用原话须另行核对原音。`summary` 在旧清单里可以省略，脚本会明确显示未提供摘要，不把 `reason` 或原文摘录冒充内容摘要。

切片清单中的 `transcript_excerpt` 优先使用与切片相交的词；裁切边界穿过某个词时保留该词并记入 `excerpt_boundary_words`，需要回听校对。缺少完整词时间时附带相交句段作为上下文，标为 `overlapping-context`，可能包含切片外的内容。该字段用于审阅，不能直接当作逐片精确字幕。

已有时间轴可离线导入，示例 `examples/timed-transcript.json`。导入精度由来源决定，仍需核实。原始识别文本与后续润色稿分开保存，未经回听不能把输出称为逐字无误的原话。

```bash
python scripts/highlight_clips.py import-transcript --job /absolute/output/实际-job-目录 --input /absolute/timed-transcript.json
```

## 交付与异常

每个源文件使用含名称和指纹的独立目录，同名文件不会互相覆盖。保留原始文件，切片使用原媒体的时间轴。

- `media.json`、`analysis.mp3`、`analysis.pcm`、`chunks/` 保存素材与预处理结果。
- `transcript.json`、`transcript.txt`、`transcript.md`、`transcript.srt` 是最近一次完整识别或导入的结果。`transcription_status.json` 记录当前状态，新运行未完成时禁止使用旧稿裁切。
- `file-asr/` 保存文件接口设置、订单状态、上传响应、轮询响应与完整结果。响应中若反射凭证会脱敏。`runs/` 保存可选 V2.0 分段状态与响应，`transcript.partial.json` 仅用于排查未完成任务。
- `clips/clips-video.json` 和 `clips/clips-audio.json` 分别保留两种模式最近一次完整结果。`clips/clips.json` 记录最近一次执行的状态与结果，开始新任务时先标为 `incomplete`。`clips.partial.json` 在执行中或失败后保留，完整成功后删除。
- `clips/highlights.md` 提供最近一次裁切的可读清单，包含标题、时间、Agent 摘要、切片链接及引用校对提示。对应的 `highlights-video.md` 与 `highlights-audio.md` 分别保留每种模式最近一次完整清单。新执行未完成时，`highlights.md` 会明确显示未完成状态，避免误用上一轮报告。

源文件发生变化后需要重新准备。首版要求音轨起点与媒体起点对齐，检测到超过 50 ms 的相对起点偏移会拒绝处理，防止转写和裁切错位。此类素材先制作时间轴对齐的新文件，再用新文件完成全部步骤。任务目录存在 `.transcribe.lock` 时检查是否仍有进程运行，确认旧进程已退出后再清理锁；不能因此绕过未知提交状态。

交付时说明高光依据、实际文件路径、时间精度与待回听边界。合成音和手工文本只用于离线流程测试，不能当作 ASR 识别成绩或真实业务案例。
