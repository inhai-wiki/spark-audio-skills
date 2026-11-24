# 录音文件转写大模型协议与恢复

核对来源：[讯飞录音文件转写大模型文档](https://www.xfyun.cn/doc/spark/asr_llm/Ifasr_llm.html)。当前 Skill 的推荐后端为此服务。短音频听写 V2.0 的 WebSocket 鉴权与参数不能用于本接口。

验证范围：已使用自制 12.58 秒双音色语音完成真实上传、两次查询、句词时间解析与本地切片。该结果说明接口链路打通；识别效果评估仍需代表目标场景的真实素材。可选 V2.0 后端仅完成协议模拟。

## 网络与签名

固定 HTTPS 主机为 `office-api-ist-dx.iflyaisol.com`，POST `/v2/upload` 上传二进制 MP3；POST `/v2/getResult` 的 body 必须为 `{}`。参数在 URL query，签名在 `signature` 请求头。脚本使用系统 TLS 校验，单次请求失败不自动重发。

签名排除 `signature` 和空值后，按字段名排序，以 Java `URLEncoder` 规则编码（空格为 `+`，`~` 为 `%7E`），组成 `key=value&...`。用 APISecret 做 HMAC-SHA1 后 Base64。`accessKeyId` 对应 APIKey。凭证和签名不得写入公开日志；脚本只保存响应与不含密钥的任务设置。

上传携带 AppID、APIKey、含时区的 `dateTime`、16 位字母数字 `signatureRandom`、实际文件大小和 MP3 时长（毫秒）。查询重新生成日期，复用该上传订单的随机串。成功要求返回明确成功码与订单号；查询还须验证订单相符、`failType=0` 和完成状态 `4`。状态 `0/3` 或明确的处理中错误码 `100013` 继续等待。未知、缺字段、空最终结果均报错。

## CLI 选项

| 选项 | 默认值与作用 |
| --- | --- |
| `--language` | `autodialect`；`autominor` 需要相应权限 |
| `--role-type` | `0` 关闭；`1` 通用角色分离；`3` 已注册声纹分离 |
| `--role-num` | `0` 盲分；可填 1–10，须开启角色分离 |
| `--feature-ids` | 角色类型 3 使用的 1–64 个已注册 ID，逗号分隔 |
| `--domain` | 可选 court、finance、medical、tech、sport、edu、isp、gov、game、ecom、mil、com、life、ent、culture、car |
| `--max-polls` | 单次进程最多查询 60 次；未完成返回退出码 2 |
| `--poll-interval` | 查询间隔 5 秒，可设 0–60 秒 |
| `--timeout` | 单次网络操作 120 秒 |
| `--resume` | 仅查询原设置下已持久化订单，绝不新上传 |
| `--retry-uncertain` | 显式允许再提交未知或被拒绝的上传；可能重复计费 |

文本顺滑与口语规整均固定为 false。识别后的编辑、摘要、高光选择由 Agent 在另外的稿件或 manifest 中完成，不覆盖原始识别文本。

## 时间轴

- `orderResult` 是 JSON 字符串；其 `lattice[].json_1best` 仍需 JSON 解码。
- `st.bg`、`st.ed` 是句子在音频中的毫秒位置。
- `ws.wb`、`ws.we` 是相对该句 `bg` 的 10 ms 帧数。词时间秒数为 `bg / 1000 + wb * 0.01`，结束类似。
- 举例：`bg=880`、`ed=1680`、`wb=22`、`we=75` 表示句范围 0.88–1.68 秒，该词为 1.10–1.63 秒。
- `cw` 只取第一候选 `w`，保留标点和原文。`rl` 作为 `speaker` 保存。若返回单独原始结果 `lattice2`，优先使用它。
- SRT 使用句范围；JSON 保存词范围与原始帧数。标点可能零时长，词边界应回听核实。
- MP3 尾部填充使句末稍超源时长时，最多接受 250 ms，规范句末裁到源结尾并标注 `end_clamped_to_source`。原始响应、`source_bg_ms/source_ed_ms` 与词帧保留用于复核。其他越界不静默修复。

## 断点恢复与边界

任务设置和上传文件哈希决定运行目录。上传前保存 `upload_started` 与随机串；成功响应先写本地，再提取并保存 `order_id`，然后才查询。进程恰在响应落盘后中断时，下次从响应恢复订单。查询错误不删除订单；恢复继续查询即可。

上传结果不明时，不存在可用订单号就拒绝 `--resume` 和普通重跑。人工确认后再考虑 `--retry-uncertain`，此前状态会归档。修改识别参数意味着新任务；不要用改参数绕过未知结果保护。`state.json` 与 `settings.json` 不保存凭证，原始响应会脱敏。任务目录含识别正文与角色信息，按素材的访问权限保存。

完整状态必须同时具备有效结果，才会复用缓存并重导出 transcript。`transcription_status.json` 未完成时，clip 拒绝使用历史稿件。同一 job 的两个云端后端共用锁，阻止并发提交。

当前实现不自动处理回调、不主动注册声纹、不调用语音分析或 LLM 摘要服务。高光选择由 Agent 读取全文后完成，ffmpeg 负责按核实时间裁切原媒体。
