# nano-agent-crewai

[nano-agent](../README.md) 的 **CrewAI** 变体：**规划**阶段使用 `Agent` + `Task` + `Crew.kickoff()`；工具执行、重试判定与 **最终报告**仍与主项目一致（报告使用 `LLMTool.final_report`，保留截断与 `data_inspection_failed` 降级）。

## 环境要求

- **Python ≥3.11**。
- **CrewAI 与 3.14**：PyPI 上 `crewai` 目前普遍声明 **仅支持 Python &lt; 3.14**。本包在 **3.14** 下安装时**不会**拉取 `crewai`，规划阶段会**自动回退**为与主项目相同的 `LLMTool.plan`（其余流程不变）。若要用到真正的 Crew 编排，请使用 **3.11–3.13** 并照常 `pip install -e .`（会安装 `crewai`）。
- 与主项目相同：OpenAI 兼容 `base_url`（如 DashScope）、Tavily 可选。

## 安装

```bash
cd nanoAgent-crewai
pip install -e .
```

## 配置

复制 `config.yaml.example` 为 `config.yaml` 并填写密钥。未填 `tavily.api_key` / `llm` 时，会尝试继承仓库根目录 `../config.yaml`（与主项目共用）。

## 使用

```bash
nano-agent-crewai run "总结当前目录" --session default --config config.yaml
nano-agent-crewai chat --session default --config config.yaml
# 或
python -m nano_agent_crewai.cli chat --session default --config config.yaml
```

## 与其它子项目对比

| 子项目 | 规划 / 编排 |
|--------|-------------|
| nano-agent | 手写循环 + `LLMTool.plan` |
| nano-agent-langchain | 手写循环 + LangChain 链 |
| nano-agent-langgraph | LangGraph 状态图 |
| **nano-agent-crewai** | **CrewAI Crew**（单规划 Agent）+ 手写工具循环 |
