# 接口实现与复核依据

核对来源为 [讯飞语音听写 V2.0 官方文档](https://static.xfyun.cn/doc/spark/spark_zh_iat_new.html)。核对日期为 2026-11-25。

- 请求 `wss://iat.cn-huabei-1.xf-yun.com/v1`，使用 `host`、GMT `date`、`GET /v1 HTTP/1.1` 组成签名原文，以 APISecret 做 HMAC-SHA256，Authorization 描述和签名分别 Base64 后作为查询参数。
- 首帧 `header.status=0`，中间帧为 1，最后空音频帧为 2。音频 `seq` 递增。
- 参数 `domain=spark_asr_v2`、`language=zh_cn`、`accent=mandarin`，音频 `raw`、16000 Hz、16 bit、单声道。
- 官方每次限制 60 秒，脚本最大分段 55 秒，默认 20 秒。按文档推荐每 40 ms 发送 1024 字节，处理已录制文件时不会承诺快于音频时长。
- `dwa=wpgs` 的 `pgs=rpl` 使用 `rg=[开始序号,结束序号]` 替换旧结果，范围两端均包含。重复的同序号更新覆盖旧值。
- `header.code` 和解码结果里的 `ret` 都需检查。`header.status=2` 标识最终响应。异常时保留已成功分段，未知请求不自动重试。
- `vinfo=1` 请求子句信息，但当前文档中 `bg/ed` 是保留字段，`ws.bg` 是 10 ms 单位的起点且可能不可用。首版不从这些字段推造逐字结束时间。

上下文 JSON 最大 2 KB，脚本将它按接口要求编码为 JSON 字符串。可以传入下列结构，内容仅作为格式示例。

```json
{
  "recent": {
    "history": [{"seq": 1, "text": "本次讨论围绕语音模型的接口接入。"}]
  },
  "bias": {
    "hotword": [{"text": "Spark-ASR", "weight": 0.9}],
    "replaces": [{"from": "示例易混名称", "to": "示例标准名称"}]
  }
}
```

不把鉴权 URL 写入日志。服务端响应和识别文本属于本次用户素材，保存在任务目录供核查。当前实现未用真实账户联调，协议单测和离线媒体测试不能替代真实识别效果验证。
