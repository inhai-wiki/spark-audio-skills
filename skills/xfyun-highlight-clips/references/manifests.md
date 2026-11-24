# 时间轴与高光文件

## 已有转写导入

`import-transcript` 接受包含 `segments` 的 JSON 对象，也接受直接的数组。每段必须有以秒计的 `start`、`end` 和 `text`。时间在原始媒体坐标系内，按照开始时间排序。可选 `id` 必须唯一。`source_sha256` 存在时必须和任务素材相符。

```json
{
  "timing_precision": "externally-aligned",
  "segments": [
    {"id": "s1", "start": 1.0, "end": 4.0, "text": "这是一段用于说明格式的文本。"},
    {"id": "s2", "start": 4.1, "end": 8.0, "text": "正式运行请使用这份媒体的真实转写。"}
  ]
}
```

导入不会调用云端 API，也不会验证文字是否与声音一致。精确程度以实际转写来源和人工校验为准。可选 `words` 数组包含 `text`、`start`、`end`，时间为原媒体绝对秒数，允许标点零时长；完整词串需对应段落文本，才能用于切片文本筛选。

## 高光清单

`clip` 读取 `clips` 数组，时间不得超出原始素材。每个 ID 必须唯一，生成文件名会过滤路径字符。`segment_ids` 引用当前 `transcript.json` 中的 ID。`source_sha256` 可从 `media.json` 拷贝，避免误用另一段素材的高光清单。

```json
{
  "clips": [
    {
      "id": "highlight-01",
      "title": "标题描述真实片段内容",
      "start": 1.0,
      "end": 8.0,
      "reason": "该片段包含完整问题、解释与结论。",
      "summary": "用一至三句概括这段原音实际讲到的问题、观点和适用条件。此处仅为字段示例，运行时由 Agent 根据对应转写填写。",
      "segment_ids": ["s1", "s2"]
    }
  ]
}
```

`title` 命名片段，`reason` 记录选择依据，`summary` 概括片段实际内容。摘要由 Agent 依据对应转写编写，保留说话人的归因、限定和不确定性。脚本只校验并保留摘要，不调用任何额外文本模型，也不把 ASR 返回视为摘要。

`summary` 是可选的非空字符串，旧清单可省略。缺失时 Markdown 会显示未提供内容摘要，不能用选择理由代替。摘要存在时，结果清单带 `summary_origin=manifest`。清单中的摘要与原始转写分开，`transcript_excerpt` 始终从 `transcript.json` 获取。

成功裁切后导出 `highlights.md`，逐段呈现标题、起止时间、内容摘要、选择理由、可点击的切片文件以及原文校对提示。链接相对于清单所在的 `clips` 目录；分发清单时应连同对应媒体文件一起保留。摘要是概括表达，直接引用还需回听原音。

当句段具有完整词时间，`transcript_excerpt` 只连接与切片相交的词，范围标为 `transcript_excerpt_scope=word-overlap`。边界穿过的词仍纳入，并列入 `excerpt_boundary_words` 供回听校对。零时长标点在切片起点之后、终点以内才纳入。缺少词时间或词串与段落文本不完整对应时，使用整个相交句段，范围标为 `overlapping-context`；同时记录 `excerpt_context_segment_ids`。混合两类来源时标为 `mixed-word-and-context`。所有摘录均带 `excerpt_review_required=true`，用作审阅辅助，发布字幕需要另行校正。

同一个片段的路径由源指纹和起止位置决定。重跑同一裁切会替换本工具生成的同名输出，输入原媒体不修改。输出 MP4 采用视频和音频重编码，避免关键帧复制导致的起点偏移。输出 MP3 会有编码器填充，程序容许 0.25 秒以内的文件时长差异。


## 两种输出模式与执行状态

视频模式的完整结果保存在 `clips-video.json`，音频模式保存在 `clips-audio.json`。先输出 MP4 再使用 `--audio-only` 时，两份清单分别保留对应文件。每种模式的固定清单记录该模式最近一次完整结果。

`clips.json` 是最近一次执行的状态与结果。新任务开始编码前会先写入 `status=incomplete` 和本次 `mode`，执行中逐步更新已完成文件。`clips.partial.json` 同时保留当前进度，失败时保留，全部文件完成并写入正式清单后删除。读取者应先检查当前状态，不能仅凭旧的某模式清单推断新任务已经成功。

`highlights.md` 与当前执行同步，失败或执行未结束时明确标注未完成。完整的 `highlights-video.md`、`highlights-audio.md` 分别保留对应模式最近一次成功报告。JSON 的 `highlights_markdown` 字段指向本次完成模式的报告。
