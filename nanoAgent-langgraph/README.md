# nano-agent-langgraph

[nano-agent](../README.md) 的 **LangGraph** 编排实现：业务语义与主项目一致（OpenAI 兼容 `LLMTool`、Tavily、受限 Shell、Markdown 会话），将「规划 → 工具 → 评估 → 重试」显式建模为状态图。

## 安装

在仓库根目录或本目录下：

```bash
cd nanoAgent-langgraph
pip install -e .
```

## 配置

复制 `config.yaml.example` 为 `config.yaml` 并填写密钥（`config.yaml` 已在 `.gitignore`）；或使用环境变量（见 `.env.example`）。

若本目录下未填 `tavily.api_key`，启动时会自动尝试读取**仓库根目录**的 `../config.yaml` 中的 Tavily 配置（与主项目共用一份密钥）。

## 使用

```bash
nano-agent-langgraph run "总结当前目录" --session default --config config.yaml
nano-agent-langgraph chat --session default --config config.yaml
```

若提示找不到 `nano-agent-langgraph`，可改用：

```bash
python -m nano_agent_langgraph.cli chat --session default --config config.yaml
```

需要联网搜索时，请在 `config.yaml` 或环境变量 `TAVILY_API_KEY` 中配置 Tavily；未配置时仍可启动程序，仅在调用搜索工具时会提示缺少密钥。

## 与主项目 / LangChain 版的差异

| 项目 | LLM 调用 | 编排 |
|------|-----------|------|
| nano-agent | `openai` + `LLMTool` | 手写 `for` 循环 |
| nano-agent-langchain | LangChain `ChatOpenAI` + 模板链 | 手写 `for` 循环 |
| **nano-agent-langgraph** | `openai` + `LLMTool`（与主项目相同） | **LangGraph `StateGraph`**（条件边 + 重试环） |

## 图结构（概要）

`load_context` → `plan` → `execute_tools` → `evaluate` →（`prepare_retry` → `plan` 或）`final_report` → `persist` → END。
