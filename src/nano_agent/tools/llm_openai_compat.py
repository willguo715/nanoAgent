from __future__ import annotations

import json
from typing import Any

from openai import OpenAI
from nano_agent.utils.retry import retry


class LLMTool:
    """OpenAI 兼容客户端封装，负责规划与最终报告生成。"""

    def __init__(self, base_url: str, api_key: str, model: str, retries: int = 3) -> None:
        """初始化 OpenAI 兼容客户端与模型参数。"""
        self.client = OpenAI(base_url=base_url, api_key=api_key)
        self.model = model
        self.retries = retries

    @retry(attempts=3, min_wait_seconds=1, max_wait_seconds=4)
    def _chat(self, messages: list[dict[str, Any]], temperature: float = 0.2) -> str:
        """发送一次聊天请求并返回文本内容，失败时按重试策略处理。"""
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=messages,  # type: ignore[arg-type]
            temperature=temperature,
        )
        return resp.choices[0].message.content or ""

    def _parse_planner_json(self, raw: str) -> dict[str, Any]:
        """解析 planner 返回文本，兼容纯 JSON 与 ```json 代码块。"""
        text = (raw or "").strip()
        if not text:
            raise json.JSONDecodeError("empty planner output", text, 0)
        # 兼容模型输出 ```json ... ``` 包裹的情况
        if "```" in text:
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                text = text[start : end + 1]
        return json.loads(text)

    def plan(
        self,
        conversation_context: str,
        user_input: str,
        image_path: str | None,
        execution_feedback: str = "",
    ) -> dict[str, Any]:
        """输出结构化决策，决定是否使用工具。"""
        prompt = f"""
你是一个 CLI Agent 的规划器。请只输出 JSON，不要输出其他内容。
字段：
- need_tools: bool
- tools: list[{{name:str, args:dict}}]
- reasoning: str

可用工具：
- tavily_search(query:str)
- shell_exec(command:str)

约束：
- shell_exec 仅用于读取本地信息，不执行写入或危险命令。
- shell_exec 命令禁止使用 &&、||、;、|、>、>>、< 等拼接/重定向符号。
- 生成尽量简单、跨平台、安全的单条命令（例如 python --version、dir、ls）。
- 如果信息足够，need_tools=false。

历史上下文：
{conversation_context}

当前输入：
{user_input}
图片路径：{image_path or "(none)"}

上轮执行反馈（若为空表示首次规划）：
{execution_feedback or "(none)"}
"""
        raw = self._chat([{"role": "user", "content": prompt}], temperature=0.1)
        try:
            return self._parse_planner_json(raw)
        except json.JSONDecodeError:
            return {"need_tools": False, "tools": [], "reasoning": f"planner parse fallback: {raw}"}

    def final_report(
        self,
        conversation_context: str,
        user_input: str,
        image_path: str | None,
        planner: dict[str, Any],
        tool_logs: list[dict[str, Any]],
    ) -> str:
        """将输入、工具证据整合为精简 Markdown 报告。"""
        prompt = f"""
你是一个专业工程助手。请输出“精简版”Markdown结果，目标是让用户快速抓重点。
硬性要求：
- 总长度控制在 220-380 汉字（不含代码块）。
- 优先级：最终结论 > 关键证据 > 下一步。
- 禁止空话、背景铺垫、重复表达。
- 不要生成大段表格，不要长篇分析。

输出结构（必须使用以下 4 节）：
1. 最终结论（1-2 句，先给答案）
2. 关键证据（2-4 条短 bullet）
3. 工具执行（仅列实际调用过的工具与核心输出）
4. 下一步（最多 3 条）

历史上下文：
{conversation_context}

用户输入：
{user_input}
图片路径：{image_path or "(none)"}

规划结果(JSON)：
{json.dumps(planner, ensure_ascii=False)}

工具日志(JSON)：
{json.dumps(tool_logs, ensure_ascii=False)}
"""
        return self._chat([{"role": "user", "content": prompt}], temperature=0.2)
