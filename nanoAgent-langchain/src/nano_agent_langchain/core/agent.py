from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

from nano_agent_langchain.memory.session_store import SessionStore
from nano_agent_langchain.tools.shell_guard import ShellGuard
from nano_agent_langchain.tools.tavily_search import TavilySearchTool
from nano_agent_langchain.utils.retry import retry

# LangChain 提示模板：变量用单花括号；字面量花括号需加倍。
_PLAN_TEMPLATE = """你是一个 CLI Agent 的规划器。请只输出 JSON，不要输出其他内容。
字段：
- need_tools: bool
- tools: list[{{"name": str, "args": dict}}]
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
图片路径：{image_path}

上轮执行反馈（若为空表示首次规划）：
{execution_feedback}
"""

_REPORT_TEMPLATE = """你是一个专业工程助手。请输出“精简版”Markdown结果，目标是让用户快速抓重点。
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
图片路径：{image_path}

规划结果(JSON)：
{planner_json}

工具日志(JSON)：
{tool_logs_json}
"""


def _parse_planner_json(raw: str) -> dict[str, Any]:
    """解析 planner 返回文本，兼容纯 JSON 与 ```json 代码块。"""
    text = (raw or "").strip()
    if not text:
        raise json.JSONDecodeError("empty planner output", text, 0)
    if "```" in text:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]
    return json.loads(text)


class LangChainAgentRunner:
    """使用 LangChain ChatOpenAI + Runnable 提示链的 Agent：规划 -> 工具 -> 报告 -> 持久化。"""

    def __init__(self, config: dict[str, Any], config_path: str = "config.yaml") -> None:
        """初始化 ChatOpenAI、会话存储、搜索与受限 shell。"""
        self.config = config
        session_dir = Path(config["agent"]["session_dir"])
        self.store = SessionStore(session_dir)
        self._retries = int(config["agent"].get("max_retries", 3))
        self.llm = ChatOpenAI(
            model=config["llm"]["model"],
            api_key=config["llm"]["api_key"] or None,
            base_url=config["llm"]["base_url"] or None,
            temperature=float(config["llm"].get("temperature", 0.2)),
        )
        # 规划阶段使用略低温度，与原版 LLMTool.plan 一致。
        self._llm_plan = self.llm.bind(temperature=0.1)
        self._plan_prompt = ChatPromptTemplate.from_template(_PLAN_TEMPLATE)
        self._plan_chain = self._plan_prompt | self._llm_plan | StrOutputParser()
        self._report_prompt = ChatPromptTemplate.from_template(_REPORT_TEMPLATE)
        self._report_chain = self._report_prompt | self.llm | StrOutputParser()

        self.search = TavilySearchTool(
            api_key=config["tavily"]["api_key"],
            max_results=config["tavily"]["max_results"],
        )
        self.shell = ShellGuard(
            workspace=Path.cwd(),
            config_path=Path(config_path),
            allowlist=config["shell"]["allowlist"],
            workspace_only=config["shell"]["workspace_only"],
        )

    @retry(attempts=3, min_wait_seconds=1, max_wait_seconds=4)
    def _invoke_plan(self, payload: dict[str, Any]) -> str:
        """调用规划链（带网络重试）。"""
        return str(self._plan_chain.invoke(payload))

    @retry(attempts=3, min_wait_seconds=1, max_wait_seconds=4)
    def _invoke_report(self, payload: dict[str, Any]) -> str:
        """调用最终报告链（带网络重试）。"""
        return str(self._report_chain.invoke(payload))

    def _context_tail(self, session_name: str) -> str:
        """提取会话尾部上下文，控制输入模型的历史长度。"""
        text = self.store.load_markdown(session_name)
        max_turns = self.config["agent"]["max_context_turns"]
        chunks = text.split("## Turn ")
        if len(chunks) <= 1:
            return text
        tail = chunks[-max_turns:]
        return "## Turn ".join(tail)

    def _run_tools(
        self,
        planner: dict[str, Any],
        progress_cb: Callable[[str], None] | None = None,
    ) -> list[dict[str, Any]]:
        """根据规划结果依次调用工具，并收集标准化日志。"""
        def emit(message: str) -> None:
            if progress_cb is not None:
                progress_cb(message)

        logs: list[dict[str, Any]] = []
        tools = planner.get("tools", [])
        emit(f"检测到 {len(tools)} 个工具调用。")
        for item in tools:
            name = item.get("name", "")
            args = item.get("args", {}) or {}
            if name == "tavily_search":
                query = str(args.get("query", "")).strip()
                emit(f"调用 tavily_search: {query}")
                try:
                    result = self.search.search(query=query)
                except Exception as exc:  # noqa: BLE001
                    result = {"ok": False, "error": str(exc)}
                logs.append({"tool": name, "summary": f"query={query}", "result": result})
                emit("tavily_search 调用完成。")
            elif name == "shell_exec":
                command = str(args.get("command", "")).strip()
                emit(f"调用 shell_exec: {command}")
                try:
                    result = self.shell.exec(command)
                except Exception as exc:  # noqa: BLE001
                    result = {"ok": False, "error": str(exc)}
                logs.append({"tool": name, "summary": f"command={command}", "result": result})
                emit("shell_exec 调用完成。")
            else:
                logs.append({"tool": name or "unknown", "summary": "unsupported tool", "result": {}})
                emit(f"跳过不支持的工具: {name or 'unknown'}")
        return logs

    def _evaluate_attempt(
        self,
        user_input: str,
        planner: dict[str, Any],
        tool_logs: list[dict[str, Any]],
    ) -> tuple[bool, str]:
        """评估本轮是否完成，并返回可供下一轮规划使用的反馈。"""
        realtime_keywords = ["今天", "现在", "几月", "几号", "几点", "时间", "日期", "版本", "本地", "目录", "文件"]
        requires_tools = any(keyword in user_input for keyword in realtime_keywords)
        if not planner.get("need_tools"):
            if requires_tools:
                return False, "task likely needs tool evidence; planner returned no-tools"
            return True, "planner decided no tools needed"
        if not tool_logs:
            return False, "planner required tools but no tool logs produced"

        for log in tool_logs:
            result = log.get("result", {})
            if isinstance(result, dict):
                if result.get("ok") is True:
                    if log.get("tool") == "shell_exec":
                        if int(result.get("returncode", 1)) == 0:
                            return True, "shell_exec completed with returncode=0"
                    else:
                        return True, f"{log.get('tool', 'tool')} returned ok=true"
                if result.get("error"):
                    return False, f"{log.get('tool', 'tool')} error: {result.get('error')}"
        return False, "all tool calls failed or returned non-success status"

    def _plan(
        self,
        conversation_context: str,
        user_input: str,
        image_path: str | None,
        execution_feedback: str = "",
    ) -> dict[str, Any]:
        """调用 LangChain 链得到规划 JSON。"""
        raw = self._invoke_plan(
            {
                "conversation_context": conversation_context,
                "user_input": user_input,
                "image_path": image_path or "(none)",
                "execution_feedback": execution_feedback or "(none)",
            }
        )
        try:
            return _parse_planner_json(raw)
        except json.JSONDecodeError:
            return {"need_tools": False, "tools": [], "reasoning": f"planner parse fallback: {raw}"}

    def _final_report(
        self,
        conversation_context: str,
        user_input: str,
        image_path: str | None,
        planner: dict[str, Any],
        tool_logs: list[dict[str, Any]],
    ) -> str:
        """生成最终 Markdown 报告。"""
        return self._invoke_report(
            {
                "conversation_context": conversation_context,
                "user_input": user_input,
                "image_path": image_path or "(none)",
                "planner_json": json.dumps(planner, ensure_ascii=False),
                "tool_logs_json": json.dumps(tool_logs, ensure_ascii=False),
            }
        )

    def run_once(
        self,
        session_name: str,
        user_input: str,
        image_path: str | None = None,
        progress_cb: Callable[[str], None] | None = None,
    ) -> str:
        """执行一轮完整任务并将过程与结果写入会话记录。"""
        def emit(message: str) -> None:
            if progress_cb is not None:
                progress_cb(message)

        emit("读取会话上下文中...")
        context = self._context_tail(session_name)
        max_attempts = int(self.config["agent"].get("max_exec_attempts", 3))
        planner: dict[str, Any] = {}
        tool_logs: list[dict[str, Any]] = []
        retry_feedback = ""
        done = False

        for attempt in range(1, max_attempts + 1):
            emit(f"开始第 {attempt}/{max_attempts} 轮规划...")
            planner = self._plan(context, user_input, image_path, execution_feedback=retry_feedback)
            if planner.get("need_tools"):
                emit("规划结果: 需要调用工具。")
                tool_logs = self._run_tools(planner, progress_cb=emit)
            else:
                emit("规划结果: 无需工具，直接生成报告。")
                tool_logs = []

            done, retry_feedback = self._evaluate_attempt(user_input, planner, tool_logs)
            if done:
                emit(f"第 {attempt} 轮执行判定成功。")
                break
            if attempt < max_attempts:
                emit(f"第 {attempt} 轮未完成，准备重试。原因: {retry_feedback}")
            else:
                emit(f"已达到最大重试次数，结束。最后原因: {retry_feedback}")

        emit("正在生成最终 Markdown 报告...")
        report = self._final_report(context, user_input, image_path, planner, tool_logs)
        emit("正在写入会话文件...")
        turn_id = self.store.load_markdown(session_name).count("## Turn ") + 1
        self.store.append_turn(
            session_name=session_name,
            turn_id=turn_id,
            user_input=user_input,
            image_path=image_path,
            planner_json=json.dumps(planner, ensure_ascii=False, indent=2),
            tool_logs=tool_logs,
            final_report=report,
        )
        emit("本轮执行完成。")
        return report
