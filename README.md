# nano-agent

一个本地运行的 Python CLI Agent，支持：

- 文本/图片输入
- OpenAI 兼容模型调用
- Tavily 搜索
- Shell 白名单工具（支持用户授权追加）
- 多轮会话与 Markdown 持久化

## 当前特性

- 过程可见：执行时会持续输出阶段进度（规划、工具调用、生成报告、写入会话）
- 自动重试：任务失败后会按配置自动重试，直到成功或达到上限
- 精简输出：最终报告默认输出重点（结论/证据/工具执行/下一步）
- 安全执行：shell 工具默认白名单，支持受限 `python -c` 安全模板

## 快速开始

1. 安装依赖

```bash
pip install -e .
```

2. 配置

复制 `config.yaml.example` 为 `config.yaml` 并填写密钥（`config.yaml` 已加入 `.gitignore`，勿提交）；或复制 `.env.example` 并通过环境变量注入。

3. 运行

```bash
nano-agent chat --session default
```

或单次执行：

```bash
nano-agent run "帮我总结当前目录结构" --session default
```

图片输入示例：

```bash
nano-agent run "描述这张图重点" --image "D:/path/demo.png" --session default
```

## 配置说明

关键配置位于 `config.yaml`：

- `llm.base_url` / `llm.api_key` / `llm.model`
- `tavily.api_key`
- `agent.max_exec_attempts`：单轮任务最大重试次数
- `shell.allowlist`：可执行命令白名单

建议优先使用环境变量注入密钥，避免把真实密钥写入仓库：

- `OPENAI_BASE_URL`
- `OPENAI_API_KEY`
- `OPENAI_MODEL`
- `TAVILY_API_KEY`

## 安全建议

- 不要提交包含真实密钥的 `config.yaml`
- 会话日志位于 `sessions/`，可能包含敏感业务信息，默认不建议上传
- 若密钥曾明文暴露，请及时在服务端轮换
