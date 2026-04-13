from __future__ import annotations

import json
from typing import Any

from openai import BadRequestError, OpenAI

from nano_agent_crewai.utils.retry import retry


def _truncate_text(text: str, max_chars: int) -> str:
    """截断过长文本，避免提示词过大或触发云端内容审核误报。"""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 12].rstrip() + "\n\n…[已截断]"


def _json_for_prompt(obj: Any, max_chars: int) -> str:
    """序列化 JSON 并限制长度。"""
    return _truncate_text(json.dumps(obj, ensure_ascii=False), max_chars)


def _compact_tool_logs_for_prompt(tool_logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    压缩工具日志：保留工具名与摘要，结果只保留短摘录，降低第三方网页正文触发审核的概率。
    """
    compact: list[dict[str, Any]] = []
    for log in tool_logs:
        name = log.get("tool", "")
        summary = log.get("summary", "")
        res = log.get("result")
        excerpt = ""
        if isinstance(res, dict):
            if res.get("ok") is False and res.get("error"):
                excerpt = str(res.get("error"))[:500]
            elif "results" in res and isinstance(res["results"], list) and res["results"]:
                parts: list[str] = []
                for item in res["results"][:3]:
                    if not isinstance(item, dict):
                        continue
                    t = (item.get("title") or "")[:120]
                    c = (item.get("content") or "")[:200]
                    if t or c:
                        parts.append(f"{t} {c}".strip())
                excerpt = " | ".join(parts)[:1200]
            else:
                excerpt = _truncate_text(json.dumps(res, ensure_ascii=False), 1500)
        else:
            excerpt = _truncate_text(str(res), 800)
        compact.append({"tool": name, "summary": summary, "result_excerpt": excerpt})
    return compact


def build_plan_user_content(
    conversation_context: str,
    user_input: str,
    image_path: str | None,
    execution_feedback: str = "",
) -> str:
    """
    构建规划阶段发给模型的用户消息正文（与原版 nano-agent 一致）。
    CrewAI 的 Task.description 与 OpenAI 直连共用同一套提示词。
    """
    feedback = (execution_feedback or "").strip()
    retry_block = ""
    if feedback and feedback != "(none)":
        retry_block = f"""
【上一轮未通过，必须按反馈修正】
反馈原文：{feedback}
若反馈指出「需要工具证据但未调用工具」：本轮必须设 need_tools=true，并至少调用一次 shell_exec 或 tavily_search；禁止再次仅返回 need_tools=false。
"""

    return f"""
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
- 在 Windows 上禁止使用 Linux 的 `date +%A`、`date +%F` 等（CMD 会卡住或超时）；请用一条 python -c 打印本机日期与星期英文缩写，例如：
  python -c "import datetime; n=datetime.datetime.now(); print(n.strftime('%Y-%m-%d'), n.strftime('%A'))"
- 当用户询问「今天/现在/几月/几号/几点/星期几/日期/时间」等**依赖本机或实时环境**的信息时，必须 need_tools=true，并用 shell_exec 获取本机日期时间，不可仅凭模型记忆回答。
- 当用户询问「本地目录/文件/版本」等需本机读取时，必须 need_tools=true。
- 仅当问题明显属于纯知识推理、与当前时间/本机环境无关时，才可 need_tools=false。
{retry_block}
历史上下文：
{conversation_context}

当前输入：
{user_input}
图片路径：{image_path or "(none)"}

上轮执行反馈（若为空表示首次规划）：
{feedback or "(none)"}
"""


class LLMTool:
    """OpenAI 兼容客户端封装，负责规划与最终报告生成。"""

    def __init__(self, base_url: str, api_key: str, model: str, retries: int = 3) -> None:
        """初始化 OpenAI 兼容客户端与模型参数。"""
        self.client = OpenAI(base_url=base_url, api_key=api_key)
        self.model = model
        self.retries = retries

    def _completion_text(self, messages: list[dict[str, Any]], temperature: float = 0.2) -> str:
        """单次补全，不做重试（供 final_report 分级降级使用）。"""
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=messages,  # type: ignore[arg-type]
            temperature=temperature,
        )
        return resp.choices[0].message.content or ""

    @retry(attempts=3, min_wait_seconds=1, max_wait_seconds=4)
    def _chat(self, messages: list[dict[str, Any]], temperature: float = 0.2) -> str:
        """发送一次聊天请求并返回文本内容，失败时按重试策略处理。"""
        return self._completion_text(messages, temperature)

    def _offline_final_report(
        self,
        user_input: str,
        planner: dict[str, Any],
        tool_logs: list[dict[str, Any]],
        detail: str,
    ) -> str:
        """模型端拒绝生成时，输出可读的本地 Markdown，避免整条链路崩溃。"""
        lines = [
            "## 最终结论",
            "",
            "云端模型在生成小结时触发了**内容安全策略**（如 `data_inspection_failed`），无法返回润色后的报告。",
            "常见原因包括：检索结果中含敏感网页片段、关键词组合过长等，**不一定表示您的问题不当**。",
            "",
            "## 关键证据（本地摘录）",
            "",
            f"- 用户问题：`{_truncate_text(user_input, 500)}`",
            f"- 规划：`need_tools={planner.get('need_tools')}`",
            "",
            "## 工具执行",
            "",
        ]
        if not tool_logs:
            lines.append("（本轮无工具日志）")
        else:
            for log in tool_logs:
                name = log.get("tool", "")
                summary = log.get("summary", "")
                res = log.get("result")
                if isinstance(res, dict) and res.get("ok") is False and res.get("error"):
                    lines.append(f"- `{name}`：{summary} → 错误：{res.get('error')}")
                else:
                    lines.append(f"- `{name}`：{summary}")
        lines.extend(
            [
                "",
                "## 下一步",
                "",
                "- 可尝试缩短问题、减少「实时/天气」等易触发检索的词，或稍后再试。",
                "- 若持续出现，请在阿里云百炼控制台查看该错误码说明或更换模型。",
                "",
                f"<details><summary>技术详情</summary>\n\n{_truncate_text(detail, 800)}\n\n</details>",
            ]
        )
        return "\n".join(lines)

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
        prompt = build_plan_user_content(
            conversation_context,
            user_input,
            image_path,
            execution_feedback=execution_feedback,
        )
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
        ctx = _truncate_text(conversation_context, 6000)
        compact_logs = _compact_tool_logs_for_prompt(tool_logs)
        planner_s = _json_for_prompt(planner, 4000)
        logs_s = _json_for_prompt(compact_logs, 12000)

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
{ctx}

用户输入：
{user_input}
图片路径：{image_path or "(none)"}

规划结果(JSON)：
{planner_s}

工具日志（已压缩摘录）：
{logs_s}
"""
        msg = [{"role": "user", "content": prompt}]
        try:
            return self._completion_text(msg, temperature=0.2)
        except BadRequestError as exc:
            # 阿里云等兼容网关：整段输入可能触发 data_inspection_failed，再试极简提示
            minimal = f"""请用简体中文写简短 Markdown（约 200 字），根据用户问题和下列要点回答，不要复述网页全文。
用户问题：{user_input}
要点：need_tools={planner.get("need_tools")}；已调用工具：{", ".join(str(x.get("tool")) for x in compact_logs) or "无"}。
"""
            try:
                return self._completion_text([{"role": "user", "content": minimal}], temperature=0.2)
            except BadRequestError as exc2:
                return self._offline_final_report(
                    user_input,
                    planner,
                    tool_logs,
                    detail=str(exc2) or str(exc),
                )
