# 参与贡献

欢迎提交缺陷修复、文档改进、可复现示例，以及媒体格式、接口错误处理和任务恢复方面的改进。

## 准备开发环境

需要 Python 3.10+、FFmpeg 和 ffprobe。FFmpeg 应包含 `libx264` 与 `libmp3lame`。

```bash
git clone https://github.com/inhai-wiki/spark-audio-skills.git
cd spark-audio-skills
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

`requirements-dev.txt` 已包含运行依赖。单元测试使用模拟协议与本地媒体，不需要真实凭证，也不应访问付费 API。

## 目录结构

| 目录 | 内容 |
| --- | --- |
| `skills/xfyun-highlight-clips/` | 高光 Skill、媒体处理及 ASR 接入 |
| `skills/xfyun-dual-voice-podcast/` | 双角色 Skill、TTS 接入及拼接 |
| `scripts/` | 项目入口、安装器、离线验证与同文试听工具 |
| `examples/` | 文稿、音色配置与公开试听文件 |
| `tests/` | 模拟接口、状态恢复和媒体行为测试 |
| `docs/` | 使用指南、案例与验证记录 |

两个 Skill 需要能独立复制使用。共用入口可放在项目根目录；不要让 Skill 运行时依赖只有仓库根目录才存在的模块。

## 提交改动

从一个具体问题出发，说明触发条件、当前结果与期望行为。修改接口协议时附对应的官方文档；修改行为时更新相关说明和有意义的测试。PR 描述应包含改动目的、验证方法以及尚未覆盖的边界。

涉及语音请求的改动应保留以下行为：

- 凭证从本地环境加载，日志和异常不包含密钥、签名 URL 或认证头。
- 请求状态先落盘，已成功结果可以复用，未知计费结果不会自动重复提交。
- 输出目录锁与缓存校验继续生效，不覆盖输入媒体或无关文件。
- 双人播客保持两个不同音色与明确的角色绑定；同文试听是独立辅助工具。
- 高光摘要与 ASR 原始识别稿分开保存，时间轴精度与内容来源可以追溯。

改动后执行相关测试。涉及媒体处理时，增加一次离线流程检查：

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q scripts skills tests
.venv/bin/python scripts/offline_demo.py --out artifacts/contribution-check
```

离线输出目录需为新建或空目录。真实 API 联调需由运行者自行配置账户与额度，不放入默认测试流程；记录请求数量、文件结果与错误码，不把文件解码通过等同于准确率或主观听感通过。

## 报告问题

在 [Issues](https://github.com/inhai-wiki/spark-audio-skills/issues) 中提供操作系统、Python／FFmpeg 版本、执行命令、预期结果和实际错误。优先使用离线生成素材或可以公开使用的小样复现。

附日志前删除凭证、签名地址、账户信息及私人文稿。不要上传 `.env`、录音原件、完整任务缓存或未获授权的第三方视频。涉及认证信息泄露时，避免在公开 Issue 中贴出原值。

## 示例与文档

新示例应标明真实输入与生成结果，AI 合成音频注明来源。媒体能正常解码、实际试听评价与模型准确率测量分别记录。带日期的新闻稿保留时间范围，修改事实内容时同时更新来源说明。

提交代码和原创文档即表示同意按本项目的 [MIT License](LICENSE) 分发相应贡献。第三方材料需遵守其自身使用条件。
