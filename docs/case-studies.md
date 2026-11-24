# 真实案例与试听

下面记录三个内容生产场景：从长视频提取高光、用同一段文字比较音色、将新闻文稿做成双人节目。所有时长来自本次生成的文件，验证日期为 **2026-11-25**。完整请求与测试说明见[验证记录](validation.md)。

## 长视频高光切片

素材来自 B 站视频[《Agent和Harness到底是什么？一个动画彻底搞懂！》](https://www.bilibili.com/video/BV1t55v6DE2v/)，视频约 **13 分 06 秒**。这种场景适合把长播客、访谈或知识视频整理成可独立收听的片段，并为每段配好分发摘要。

处理时先从本地视频提取音轨，转为 16 kHz 单声道 MP3；录音文件转写大模型返回 **75 个句段**及词时间。Agent 阅读识别稿，选出三处完整观点，把内容摘要、选择理由与时间写入高光清单。脚本据此裁出 MP4 和 MP3，并生成 Markdown 结果页。

| 高光 | 原片位置 | 音频时长 | 内容摘要 |
| --- | --- | --- | --- |
| Agent 怎样循环调用工具 | 02:13.80–03:22.60 | 68.80 秒 | 视频用素材搜索与视觉检查举例，说明 Agent 如何先判断任务需要，再调用工具、观察结果，并继续循环，直到形成交付结果。 |
| 上下文压缩会丢掉什么 | 07:01.10–07:51.20 | 50.10 秒 | 上下文窗口容量有限，对话变长后需要把历史内容压缩成摘要。这能释放空间，也可能丢失先前的要求，使智能体重复已经纠正过的错误。 |
| Harness 负责哪些工程工作 | 11:41.25–12:40.10 | 58.85 秒 | 视频把输入过滤、格式清洗、参数校验、工具调用、输出过滤和错误重试归入 Harness，说明这些外围工程如何配合 Agent 的任务执行。 |

这次完成了 **1 次上传与 4 次结果查询**。三份音频与三份视频通过完整解码检查，源视频指纹保持不变。第三段视频因帧长为 58.866666 秒，表中列的是音频时长。

原始识别稿中仍有需要校对的技术名词。没有建立人工逐字参考稿，因此未计算该视频的 CER／WER；解码通过只说明文件可读取。摘要由 Agent 根据转写编写，不能作为 ASR 原生摘要能力或逐字引语使用。

仓库仅提供来源链接、时间位置与摘要，**不分发该视频原文件或剪辑素材**。复现操作时，请准备自己可使用的本地文件，参照[高光操作指南](quickstart.md#从本地视频提取高光)。

## 四音色同文试听

给栏目选声音时，先让候选音色朗读同一段文字，更容易比较节奏、数字读法和句尾处理。本次使用同一段 **92 字符**新闻文字，语速、音量、音高均为 **50**；字符数包含标点。

配置见 [news-voice-auditions-four.json](../examples/news-voice-auditions-four.json)。以下均为 **AI 合成声音**，可点击 MP3 试听。

| 音色 | 接口代码 | 时长 | 试听 |
| --- | --- | --- | --- |
| 聆飞逸 | `x6_lingfeiyi_pro` | 18.44 秒 | [播放或下载 MP3](../examples/media/voice-lingfeiyi.mp3) |
| 聆玉言 | `x6_lingyuyan_pro` | 18.12 秒 | [播放或下载 MP3](../examples/media/voice-lingyuyan.mp3) |
| 聆小璇 | `x6_lingxiaoxuan_pro` | 19.12 秒 | [播放或下载 MP3](../examples/media/voice-lingxiaoxuan.mp3) |
| 聆小玥 | `x6_lingxiaoyue_pro` | 20.08 秒 | [播放或下载 MP3](../examples/media/voice-lingxiaoyue.mp3) |

四个成功请求合计 **368 字符**。相同参数下，各音色的实际长度仍有差异；长度不用于评价音色优劣。该组文件完成了解码检查，未进行盲听打分。

初始候选中的聆玉昭 `x6_lingyuzhao_pro` 返回 **10163** 参数拒绝，没有生成成功音频。程序在该请求处停止，未自动重试；尚未提交的聆小玥在后续独立批次完成。上述四音色配置仅包含成功样本，其他账户使用时仍需确认发音人授权与接口支持。

复现命令会调用 TTS 并使用对应额度：

```bash
.venv/bin/python scripts/voice_audition.py examples/news-voice-auditions-four.json --out outputs/auditions
```

## 娱乐化双人新闻播客

这类场景适合连续制作科技新闻、人物故事和社会话题短节目。先准备已经核对的事实与来源，再让 Agent 改成短轮次对话：主持人交代事件和信息边界，另一位角色回应、追问，并加入与事件相符的评论。

本次以黄仁勋相关新闻制作职场吐槽式对谈，用「人事通知留个撤回键」串联节目。固定聆飞逸与聆玉言两种声音，编排 **12 次交替发言**，输入 **620 字符**，轮次间停顿 **250 毫秒**，最终音频 **125.15 秒**。

**AI 合成双人节目**：[播放或下载 MP3](../examples/media/jensen-news-comedy.mp3) · [查看完整文稿](../examples/jensen-news-comedy.json)

短轮次脚本给每位角色分配具体任务，也便于单独修改某一句。一个栏目确定角色和声音后，可以保留配置，更换每期文稿；在同一输出目录使用 `--resume` 时，未变的成功片段会从缓存复用。

```bash
.venv/bin/python scripts/run.py podcast validate examples/jensen-news-comedy.json
.venv/bin/python scripts/run.py podcast plan examples/jensen-news-comedy.json --out outputs/news-plan.json
.venv/bin/python scripts/run.py podcast render examples/jensen-news-comedy.json --out outputs/news
```

这次 12 段合成均成功，导出了 MP3、WAV、段级字幕、角色文稿与时间轴。MP3 和 WAV 完整解码通过，尚未将主观听感作为自动验收指标。

### 文稿时间与新闻来源

示例复盘美国当地时间 **2026 年 11 月 23 日**的新闻与交易结果，核验日期为 **2026 年 11 月 25 日**。它是一份带日期的示例文稿，不作实时播报。来源如下，便于读者核对：

| 文稿涉及的信息 | 来源与口径 |
| --- | --- |
| 11 月 12 日公开宣布 CEO 更替 | [英伟达 CEO 更替公告](https://nvidianews.nvidia.com/news/nvidia-announces-ceo-transition)；辞职实际生效日期为 11 月 11 日，见 [SEC 8-K Item 5.02](https://www.sec.gov/Archives/edgar/data/1045810/000104581026000082/nvda-20261111.htm)。 |
| 11 月 23 日公布恢复职务及调查仍在继续 | [英伟达复职公告](https://investor.nvidia.com/news/press-release-details/2026/NVIDIA-Board-Reinstates-Jensen-Huang-as-President-CEO-and-Board-Director/default.aspx)。文稿按公告日期表述。 |
| NVDA 两个交易日收盘价与约 31.5% 涨幅 | [MarketScreener 历史行情](https://ca.marketscreener.com/quote/stock/NVIDIA-CORPORATION-57355629/quotes/)及 [EFE 署名报道](https://gestion.pe/mundo/internacional/nvidia-logra-su-mayor-subida-en-23-anos-tras-restituir-a-jensen-huang-como-ceo-noticia/)。11 月 20 日为 125.53 美元，11 月 23 日为 165.05 美元，收盘价差约 31.48%。 |
| 同日整体市场反弹 | [美联社收盘综述的 WPTF 刊载版](https://www.wptf.com/2026/11/23/us-stocks-surge-and-recover-half-of-last-weeks-plunge-after-jensen-huang-returns-as-nvidias-ceo/)。文稿交代市场背景，不把个股涨幅完全归因于人事变动。 |

行情页面会滚动更新，再次制作或发布时应复核指定日期。节目中的欢送横幅、工作群与邮件属于虚构主持人的职场类比，不作为英伟达内部实际细节；合成声音也未复刻新闻人物。语音生成结果本身不验证新闻事实。

## 把案例用于自己的内容

长视频场景可沿用「本地文件 → 转写 → Agent 选段与摘要 → 裁切」的流程。新闻节目可沿用「核对文稿 → 双角色编排 → 试听选声 → 分段合成 → 拼接」的流程。每个素材或节目保留独立任务目录，就能定位识别稿、成品、缓存和请求状态。

从依赖安装到失败恢复的具体步骤见[快速开始](quickstart.md)。上述样本验证了这些流程的实际输出，未覆盖高并发稳定性、全部口音或复杂噪声条件。
