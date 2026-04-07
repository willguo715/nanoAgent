from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class SessionStore:
    """会话存储：将每轮记录持久化到 Markdown 文件中。"""

    base_dir: Path

    def __post_init__(self) -> None:
        """初始化时确保会话目录存在。"""
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, session_name: str) -> Path:
        """根据会话名返回对应的 Markdown 文件路径。"""
        return self.base_dir / f"{session_name}.md"

    def load_markdown(self, session_name: str) -> str:
        """读取会话 Markdown；若不存在则返回默认标题。"""
        file_path = self.path_for(session_name)
        if not file_path.exists():
            return "# Session Log\n\n"
        return file_path.read_text(encoding="utf-8")

    def append_turn(
        self,
        session_name: str,
        turn_id: int,
        user_input: str,
        image_path: str | None,
        planner_json: str,
        tool_logs: list[dict[str, Any]],
        final_report: str,
    ) -> None:
        """把单轮信息写入 markdown，保证过程可审计可复盘。"""
        file_path = self.path_for(session_name)
        lines: list[str] = [
            f"## Turn {turn_id}",
            "",
            "### User Input",
            "",
            user_input,
            "",
            "### Image",
            "",
            image_path or "(none)",
            "",
            "### Planner Decision (JSON)",
            "",
            "```json",
            planner_json.strip(),
            "```",
            "",
            "### Tool Calls",
            "",
        ]
        if not tool_logs:
            lines.extend(["(none)", ""])
        else:
            for log in tool_logs:
                lines.append(f"- `{log.get('tool', 'unknown')}`: {log.get('summary', '')}")
            lines.append("")
        lines.extend(
            [
                "### Final Markdown Report",
                "",
                final_report.strip(),
                "",
                "---",
                "",
            ]
        )
        file_path.write_text(
            self.load_markdown(session_name) + "\n".join(lines),
            encoding="utf-8",
        )
