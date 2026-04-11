from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from nano_agent_langgraph.memory.session_store import SessionStore
from nano_agent_langgraph.tools.llm_openai_compat import LLMTool
from nano_agent_langgraph.tools.shell_guard import ShellGuard
from nano_agent_langgraph.tools.tavily_search import TavilySearchTool


class AgentState(TypedDict):
    """LangGraph 单轮执行状态（与原版 AgentRunner 循环语义对齐）。"""

    session_name: str
    user_input: str
    image_path: str | None
    context: str
    planner: dict[str, Any]
    tool_logs: list[dict[str, Any]]
    retry_feedback: str
    attempt: int
    eval_done: bool
    final_report: str


class LangGraphAgentRunner:
    """使用 LangGraph 状态机编排：加载上下文 → 规划 → 工具 → 评估 →（重试或）报告 → 持久化。"""

    def __init__(self, config: dict[str, Any], config_path: str = "config.yaml") -> None:
        self.config = config
        self._config_path = config_path
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
        self._progress_cb: Callable[[str], None] | None = None
        self._graph = self._build_graph()

    def _emit(self, message: str) -> None:
        if self._progress_cb is not None:
            self._progress_cb(message)

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
    ) -> list[dict[str, Any]]:
        """根据规划结果依次调用工具，并收集标准化日志。"""
        logs: list[dict[str, Any]] = []
        tools = planner.get("tools", [])
        self._emit(f"检测到 {len(tools)} 个工具调用。")
        for item in tools:
            name = item.get("name", "")
            args = item.get("args", {}) or {}
            if name == "tavily_search":
                query = str(args.get("query", "")).strip()
                self._emit(f"调用 tavily_search: {query}")
                try:
                    result = self.search.search(query=query)
                except Exception as exc:  # noqa: BLE001
                    result = {"ok": False, "error": str(exc)}
                logs.append({"tool": name, "summary": f"query={query}", "result": result})
                self._emit("tavily_search 调用完成。")
            elif name == "shell_exec":
                command = str(args.get("command", "")).strip()
                self._emit(f"调用 shell_exec: {command}")
                try:
                    result = self.shell.exec(command)
                except Exception as exc:  # noqa: BLE001
                    result = {"ok": False, "error": str(exc)}
                logs.append({"tool": name, "summary": f"command={command}", "result": result})
                self._emit("shell_exec 调用完成。")
            else:
                logs.append({"tool": name or "unknown", "summary": "unsupported tool", "result": {}})
                self._emit(f"跳过不支持的工具: {name or 'unknown'}")
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

    def _node_load_context(self, state: AgentState) -> dict[str, Any]:
        """读取会话尾部作为模型上下文，并初始化本轮重试计数。"""
        self._emit("读取会话上下文中...")
        ctx = self._context_tail(state["session_name"])
        return {"context": ctx, "attempt": 1, "retry_feedback": ""}

    def _node_plan(self, state: AgentState) -> dict[str, Any]:
        """调用 LLM 生成规划 JSON。"""
        max_a = int(self.config["agent"].get("max_exec_attempts", 3))
        self._emit(f"开始第 {state['attempt']}/{max_a} 轮规划...")
        planner = self.llm.plan(
            state["context"],
            state["user_input"],
            state["image_path"],
            execution_feedback=state["retry_feedback"],
        )
        if planner.get("need_tools"):
            self._emit("规划结果: 需要调用工具。")
        else:
            self._emit("规划结果: 无需工具，直接生成报告。")
        return {"planner": planner}

    def _node_execute_tools(self, state: AgentState) -> dict[str, Any]:
        """按需执行工具并写入 tool_logs。"""
        planner = state["planner"]
        if planner.get("need_tools"):
            tool_logs = self._run_tools(planner)
        else:
            tool_logs = []
        return {"tool_logs": tool_logs}

    def _node_evaluate(self, state: AgentState) -> dict[str, Any]:
        """判定本轮是否成功；失败时更新 retry_feedback 供下一轮规划使用。"""
        done, feedback = self._evaluate_attempt(state["user_input"], state["planner"], state["tool_logs"])
        if done:
            self._emit(f"第 {state['attempt']} 轮执行判定成功。")
            return {"eval_done": True, "retry_feedback": state["retry_feedback"]}
        max_a = int(self.config["agent"].get("max_exec_attempts", 3))
        if state["attempt"] < max_a:
            self._emit(f"第 {state['attempt']} 轮未完成，准备重试。原因: {feedback}")
        else:
            self._emit(f"已达到最大重试次数，结束。最后原因: {feedback}")
        return {"eval_done": False, "retry_feedback": feedback}

    def _node_prepare_retry(self, state: AgentState) -> dict[str, Any]:
        """进入下一轮规划前递增 attempt。"""
        return {"attempt": state["attempt"] + 1}

    def _node_final_report(self, state: AgentState) -> dict[str, Any]:
        """生成最终 Markdown 报告。"""
        self._emit("正在生成最终 Markdown 报告...")
        report = self.llm.final_report(
            state["context"],
            state["user_input"],
            state["image_path"],
            state["planner"],
            state["tool_logs"],
        )
        return {"final_report": report}

    def _node_persist(self, state: AgentState) -> dict[str, Any]:
        """写入会话 Markdown。"""
        self._emit("正在写入会话文件...")
        session_name = state["session_name"]
        turn_id = self.store.load_markdown(session_name).count("## Turn ") + 1
        self.store.append_turn(
            session_name=session_name,
            turn_id=turn_id,
            user_input=state["user_input"],
            image_path=state["image_path"],
            planner_json=json.dumps(state["planner"], ensure_ascii=False, indent=2),
            tool_logs=state["tool_logs"],
            final_report=state["final_report"],
        )
        self._emit("本轮执行完成。")
        return {}

    def _route_after_evaluate(self, state: AgentState) -> Literal["prepare_retry", "final_report"]:
        """评估后分支：成功或用尽次数 → 报告；否则准备重试。"""
        if state["eval_done"]:
            return "final_report"
        max_a = int(self.config["agent"].get("max_exec_attempts", 3))
        if state["attempt"] < max_a:
            return "prepare_retry"
        return "final_report"

    def _build_graph(self) -> Any:
        """编译状态图：与原版 for 循环等价。"""
        g = StateGraph(AgentState)
        g.add_node("load_context", self._node_load_context)
        g.add_node("plan", self._node_plan)
        g.add_node("execute_tools", self._node_execute_tools)
        g.add_node("evaluate", self._node_evaluate)
        g.add_node("prepare_retry", self._node_prepare_retry)
        g.add_node("final_report", self._node_final_report)
        g.add_node("persist", self._node_persist)

        g.add_edge(START, "load_context")
        g.add_edge("load_context", "plan")
        g.add_edge("plan", "execute_tools")
        g.add_edge("execute_tools", "evaluate")
        g.add_conditional_edges(
            "evaluate",
            self._route_after_evaluate,
            {"prepare_retry": "prepare_retry", "final_report": "final_report"},
        )
        g.add_edge("prepare_retry", "plan")
        g.add_edge("final_report", "persist")
        g.add_edge("persist", END)
        return g.compile()

    def run_once(
        self,
        session_name: str,
        user_input: str,
        image_path: str | None = None,
        progress_cb: Callable[[str], None] | None = None,
    ) -> str:
        """执行一轮完整任务并将过程与结果写入会话记录。"""
        self._progress_cb = progress_cb
        initial: AgentState = {
            "session_name": session_name,
            "user_input": user_input,
            "image_path": image_path,
            "context": "",
            "planner": {},
            "tool_logs": [],
            "retry_feedback": "",
            "attempt": 1,
            "eval_done": False,
            "final_report": "",
        }
        # 规划↔评估可能多轮，默认 recursion_limit 偏低时易触发 GraphRecursionError
        max_attempts = int(self.config["agent"].get("max_exec_attempts", 3))
        limit = max(25, 6 * max_attempts + 12)
        out = self._graph.invoke(initial, config={"recursion_limit": limit})
        self._progress_cb = None
        return str(out.get("final_report", ""))
