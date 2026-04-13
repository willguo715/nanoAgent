from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from nano_agent_crewai.memory.session_store import SessionStore
from nano_agent_crewai.tools.llm_openai_compat import LLMTool, build_plan_user_content
from nano_agent_crewai.tools.shell_guard import ShellGuard
from nano_agent_crewai.tools.tavily_search import TavilySearchTool


def _crew_kickoff_raw(result: Any) -> str:
    """从 CrewAI kickoff 返回值中取出文本。"""
    if result is None:
        return ""
    raw = getattr(result, "raw", None)
    if raw is not None and str(raw).strip():
        return str(raw)
    return str(result)


class CrewAIAgentRunner:
    """
    使用 CrewAI 的 Agent + Task + Crew 完成「规划」步骤；工具执行与判定仍由 Python 编排。
    最终报告沿用 LLMTool.final_report，以保持与主项目一致的截断与 400 降级逻辑。
    """

    def __init__(self, config: dict[str, Any], config_path: str = "config.yaml") -> None:
        self.config = config
        self._config_path = config_path
        session_dir = Path(config["agent"]["session_dir"])
        self.store = SessionStore(session_dir)
        self._llm = LLMTool(
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
        # 仅在已安装 crewai 且成功创建时缓存（Python 3.14 环境通常无 crewai 包）
        self._crew_llm_plan: Any = None

    def _get_crew_llm_plan(self) -> Any:
        """规划阶段使用略低温度，与 LLMTool.plan 一致。"""
        if self._crew_llm_plan is None:
            from crewai import LLM as CrewLLM  # type: ignore[import-not-found]

            base = (self.config["llm"].get("base_url") or "").strip() or None
            self._crew_llm_plan = CrewLLM(
                model=self.config["llm"]["model"],
                api_key=self.config["llm"]["api_key"],
                base_url=base,
                temperature=0.1,
            )
        return self._crew_llm_plan

    def _context_tail(self, session_name: str) -> str:
        """提取会话尾部上下文，控制输入模型的历史长度。"""
        text = self.store.load_markdown(session_name)
        max_turns = self.config["agent"]["max_context_turns"]
        chunks = text.split("## Turn ")
        if len(chunks) <= 1:
            return text
        tail = chunks[-max_turns:]
        return "## Turn ".join(tail)

    def _plan_with_crew(
        self,
        context: str,
        user_input: str,
        image_path: str | None,
        execution_feedback: str,
    ) -> dict[str, Any]:
        """通过 CrewAI 单 Agent 单任务生成规划 JSON；未安装 crewai 或失败时回退到 LLMTool.plan。"""
        try:
            from crewai import Agent, Crew, Process, Task  # type: ignore[import-not-found]
        except ImportError:
            return self._llm.plan(context, user_input, image_path, execution_feedback=execution_feedback)

        description = build_plan_user_content(
            context,
            user_input,
            image_path,
            execution_feedback=execution_feedback,
        )
        llm = self._get_crew_llm_plan()
        planner_agent = Agent(
            role="CLI 任务规划器",
            goal="只输出一个合法 JSON 对象，描述 need_tools、tools、reasoning，不要输出其他说明文字。",
            backstory="你熟悉 JSON 与本地/网络工具约束，拒绝输出 Markdown 解释，只交付可解析的结构化结果。",
            llm=llm,
            tools=[],
            verbose=False,
            allow_delegation=False,
        )
        task = Task(
            description=description,
            expected_output="仅输出可解析的 JSON 对象，包含 need_tools、tools、reasoning 键；不要 markdown 围栏。",
            agent=planner_agent,
        )
        crew = Crew(
            agents=[planner_agent],
            tasks=[task],
            process=Process.sequential,
            verbose=False,
        )
        try:
            result = crew.kickoff()
            raw = _crew_kickoff_raw(result)
        except Exception:
            return self._llm.plan(context, user_input, image_path, execution_feedback=execution_feedback)
        try:
            return self._llm._parse_planner_json(raw)
        except json.JSONDecodeError:
            return {"need_tools": False, "tools": [], "reasoning": f"planner parse fallback: {raw}"}

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
            if not isinstance(result, dict):
                continue
            name = log.get("tool", "unknown")

            if result.get("ok") is False and result.get("error"):
                return False, f"{name} error: {result.get('error')}"

            if name == "tavily_search":
                return True, "tavily_search completed"

            if name == "shell_exec":
                if result.get("ok") is True and int(result.get("returncode", 1)) == 0:
                    return True, "shell_exec completed with returncode=0"
                continue

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
            emit(f"开始第 {attempt}/{max_attempts} 轮规划（CrewAI）...")
            planner = self._plan_with_crew(context, user_input, image_path, retry_feedback)
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
        report = self._llm.final_report(context, user_input, image_path, planner, tool_logs)
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
