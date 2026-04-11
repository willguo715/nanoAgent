# nano-agent-langchain

[nano-agent](../README.md) 的 **LangChain** 重写版：同等 CLI 与行为（规划 → 工具 → 报告 → Markdown 会话），底层使用 `langchain_openai.ChatOpenAI` 与 `Runnable` 提示链。

## 依赖

- LangChain（`ChatOpenAI`、`ChatPromptTemplate`、`StrOutputParser`）
- OpenAI 兼容 API、tavily-python、与原项目一致的 Shell 白名单逻辑

## 安装

在仓库根目录或本目录下：

```bash
cd nanoAgent-langchain
pip install -e .
```

## 配置

复制 `config.yaml.example` 为 `config.yaml` 并填写密钥（`config.yaml` 已在 `.gitignore`）；或使用环境变量（见 `.env.example`）。

若本目录下未填 `tavily.api_key`，启动时会自动尝试读取**仓库根目录**的 `../config.yaml` 中的 Tavily 配置（与主项目 `nano-agent` 共用一份密钥）。

## 使用

```bash
nano-agent-langchain run "总结当前目录" --session default --config config.yaml
nano-agent-langchain chat --session default --config config.yaml
```

若提示找不到 `nano-agent-langchain`，说明 Python 的 `Scripts` 目录未加入 PATH。可改用：

```bash
python -m nano_agent_langchain.cli chat --session default --config config.yaml
```

需要联网搜索时，请在 `config.yaml` 或环境变量 `TAVILY_API_KEY` 中配置 Tavily；未配置时仍可启动程序，仅在调用搜索工具时会提示缺少密钥。

## 与原版的差异

- LLM 调用通过 **LangChain** 的 `ChatOpenAI` + 提示模板链完成；业务仍为「JSON 规划 → 执行工具 → 判定重试 → 最终报告」。
- 会话存储、Shell 白名单、Tavily 封装与原逻辑对齐，便于对照学习两种实现。
