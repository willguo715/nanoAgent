from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


def _merge_env(cfg: dict[str, Any]) -> dict[str, Any]:
    """将环境变量覆盖到配置，方便本地开发快速切换。"""
    cfg["llm"]["base_url"] = os.getenv("OPENAI_BASE_URL", cfg["llm"]["base_url"])
    cfg["llm"]["api_key"] = os.getenv("OPENAI_API_KEY", cfg["llm"]["api_key"])
    cfg["llm"]["model"] = os.getenv("OPENAI_MODEL", cfg["llm"]["model"])
    cfg["tavily"]["api_key"] = os.getenv("TAVILY_API_KEY", cfg["tavily"]["api_key"])
    return cfg


def load_config(path: str = "config.yaml") -> dict[str, Any]:
    """读取 YAML 配置并完成必要兜底。"""
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    content = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
    cfg = _merge_env(content)
    cfg.setdefault("llm", {})
    cfg["llm"].setdefault("temperature", 0.2)
    cfg.setdefault("tavily", {})
    cfg["tavily"].setdefault("max_results", 5)
    cfg.setdefault("shell", {})
    cfg["shell"].setdefault("workspace_only", True)
    cfg["shell"].setdefault("allowlist", [])
    cfg.setdefault("agent", {})
    cfg["agent"].setdefault("session_dir", "sessions")
    cfg["agent"].setdefault("max_context_turns", 6)
    cfg["agent"].setdefault("max_retries", 3)
    cfg["agent"].setdefault("max_exec_attempts", 3)
    return cfg
