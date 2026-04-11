from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

import yaml


class ShellGuard:
    """受限 shell 执行器：白名单 + 工作目录限制 + 用户授权更新。"""

    def __init__(self, workspace: Path, config_path: Path, allowlist: list[str], workspace_only: bool = True) -> None:
        """初始化 shell 安全策略与配置持久化路径。"""
        self.workspace = workspace.resolve()
        self.config_path = config_path
        self.allowlist = allowlist
        self.workspace_only = workspace_only

    def _is_safe_python_c_template(self, command: str) -> bool:
        """判断命令是否为受限的 python -c 安全模板。"""
        text = command.strip()
        match = re.match(r"^python\s+-c\s+(['\"])(.*)\1\s*$", text, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            return False
        code = match.group(2).lower()
        # 允许以 print 为主的只读表达式；拒绝明显危险能力。
        required_keywords = ["print("]
        blocked_keywords = [
            "open(",
            "exec(",
            "eval(",
            "compile(",
            "subprocess",
            "os.system",
            "pathlib",
            "socket",
            "requests",
            "__import__",
            "input(",
        ]
        return all(k in code for k in required_keywords) and all(k not in code for k in blocked_keywords)

    def _is_allowed(self, command: str) -> bool:
        """判断命令是否命中白名单（命令头或完整模板）。"""
        if self._is_safe_python_c_template(command):
            return True
        # 匹配首个命令或完整短命令模板（例如 python --version）
        tokens = shlex.split(command)
        if not tokens:
            return False
        head = tokens[0].lower()
        if head in {x.lower() for x in self.allowlist}:
            return True
        return command.strip().lower() in {x.lower() for x in self.allowlist}

    def _safe_guard(self, command: str) -> None:
        """执行高风险关键字检查，不通过时直接拒绝。"""
        if self._is_safe_python_c_template(command):
            # 对受限 python -c 模板放行分号，支持简单多语句查询。
            banned = ["&&", "||", ">", ">>", "<", "|", "rm ", "del ", "rmdir ", "move ", "rename ", "curl ", "wget "]
        else:
            banned = ["&&", "||", ";", ">", ">>", "<", "|", "rm ", "del ", "rmdir ", "move ", "rename ", "curl ", "wget "]
        # 禁止高风险符号与关键词，防止命令拼接注入。
        lowered = f" {command.lower()} "
        for bad in banned:
            if bad in lowered:
                raise PermissionError(f"命令包含风险片段，已拒绝: {bad}")

    def _prompt_allow(self, command: str) -> bool:
        """在命令未命中白名单时，交互询问用户是否放行。"""
        answer = input(f"命令 `{command}` 不在白名单。是否允许本次执行? (y/N): ").strip().lower()
        if answer != "y":
            return False
        persist = input("是否写入白名单以便后续直接执行? (y/N): ").strip().lower()
        if persist == "y":
            self.allowlist.append(command.strip())
            self._persist_allowlist()
        return True

    def _persist_allowlist(self) -> None:
        """将当前白名单写回配置文件，便于后续复用。"""
        cfg = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        cfg.setdefault("shell", {})
        cfg["shell"]["allowlist"] = sorted(set(self.allowlist), key=str.lower)
        self.config_path.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")

    def _rewrite_windows_date_command(self, command: str) -> str:
        """
        Windows CMD 不支持 GNU 的 `date +FORMAT`，模型常误用会导致卡住直至超时。
        将类 Unix 的 date 写法改写为受限 python -c（与 _is_safe_python_c_template 一致）。
        """
        if os.name != "nt":
            return command
        text = command.strip()
        # GNU date：date +%A、date +%F 等
        if re.match(r"^date\s+\+", text, flags=re.IGNORECASE):
            return (
                'python -c "import datetime; n=datetime.datetime.now(); '
                "print(n.strftime('%Y-%m-%d'), n.strftime('%A'))\""
            )
        # 裸 `date` 在 CMD 下可能进入交互式修改日期，长时间阻塞
        if re.match(r"^date\s*$", text, flags=re.IGNORECASE):
            return "date /t"
        return command

    def exec(self, command: str) -> dict[str, Any]:
        """在安全校验后执行命令并返回标准化执行结果。"""
        command = self._rewrite_windows_date_command(command)
        self._safe_guard(command)
        if not self._is_allowed(command) and not self._prompt_allow(command):
            return {"ok": False, "error": "command not allowed by user"}
        cwd = self.workspace if self.workspace_only else None
        proc = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            shell=True,
            text=True,
            capture_output=True,
            timeout=20,
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
        }
