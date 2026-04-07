from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from nano_agent.memory.session_store import SessionStore
from nano_agent.tools.llm_openai_compat import LLMTool
from nano_agent.tools.tavily_search import TavilySearchTool
from nano_agent.tools.shell_guard import ShellGuard


class AgentRunner:
    """Agent 主编排：规划 -> 工具调用 -> 报告 -> 持久化。"""

    def __init__(self, config: dict[str, Any], config_path: str = "config.yaml") -> None:
        """初始化核心依赖，包括会话存储、LLM、搜索与受限 shell 工具。"""
        self.config = config
        session_dir = Path(config["agent"]["session_dir"])
        self.store = SessionStore(session_dir)
        self.llm = LLMTool(
            base_url=config["llm"]["base_url"],
            api_key=config["llm"]["api_key"],
            model=config["llm"]["model"],
            retries=config["agent"]["max_retries"],
        )
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

    def _context_tail(self, session_name: str) -> str:
        """提取会话尾部上下文，控制输入模型的历史长度。"""
        text = self.store.load_markdown(session_name)
        max_turns = self.config["agent"]["max_context_turns"]
        # 简单策略：截取尾部内容作为上下文；若后续需要可升级为摘要+N轮。
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
                    # shell_exec 需要 returncode=0 作为成功信号
                    if log.get("tool") == "shell_exec":
                        if int(result.get("returncode", 1)) == 0:
                            return True, "shell_exec completed with returncode=0"
                    else:
                        return True, f"{log.get('tool', 'tool')} returned ok=true"
                if result.get("error"):
                    return False, f"{log.get('tool', 'tool')} error: {result.get('error')}"
        return False, "all tool calls failed or returned non-success status"

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
            planner = self.llm.plan(context, user_input, image_path, execution_feedback=retry_feedback)
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
        report = self.llm.final_report(context, user_input, image_path, planner, tool_logs)
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
