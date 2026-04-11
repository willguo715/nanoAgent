from __future__ import annotations

import os
import warnings
from pathlib import Path
from typing import Any

import yaml


def _package_project_root() -> Path:
    """本文件位于 src/nano_agent_langgraph/config.py，项目根为 nanoAgent-langgraph 目录。"""
    return Path(__file__).resolve().parents[2]


def _resolve_config_file(requested: str) -> tuple[Path, bool]:
    """
    解析要加载的配置文件路径。

    当默认的 config.yaml 不存在时，依次尝试：
    同目录下的 config.yaml.example；子项目根目录下的 config.yaml / config.yaml.example。

    返回 (绝对路径, 是否使用了 *.example 模板)。
    """
    p = Path(requested)
    if p.is_file():
        return p.resolve(), False

    # 同目录示例：./config.yaml -> ./config.yaml.example
    co_example = p.parent / "config.yaml.example"
    if p.name == "config.yaml" and co_example.is_file():
        return co_example.resolve(), True

    # 仅当请求的是默认文件名时，回退到子项目根（便于在仓库根目录执行 python -m）
    if p.name == "config.yaml":
        root = _package_project_root()
        for cand in (root / "config.yaml", root / "config.yaml.example"):
            if cand.is_file():
                return cand.resolve(), cand.name.endswith(".example")

    raise FileNotFoundError(
        f"配置文件不存在: {requested}。"
        " 请将 nanoAgent-langgraph/config.yaml.example 复制为 config.yaml 并填写密钥，"
        "或使用 --config 指定已有配置文件路径。"
    )


def _merge_env(cfg: dict[str, Any]) -> dict[str, Any]:
    """将环境变量覆盖到配置，方便本地开发快速切换。"""
    cfg.setdefault("llm", {})
    cfg.setdefault("tavily", {})
    cfg["llm"]["base_url"] = os.getenv("OPENAI_BASE_URL", cfg["llm"].get("base_url", ""))
    cfg["llm"]["api_key"] = os.getenv("OPENAI_API_KEY", cfg["llm"].get("api_key", ""))
    cfg["llm"]["model"] = os.getenv("OPENAI_MODEL", cfg["llm"].get("model", ""))
    cfg["tavily"]["api_key"] = os.getenv("TAVILY_API_KEY", cfg["tavily"].get("api_key", ""))
    return cfg


def _inherit_tavily_from_repo_root_if_empty(cfg: dict[str, Any], loaded_path: Path) -> None:
    """
    子项目 nanoAgent-langgraph 的 config.yaml 若未填 tavily.api_key，
    自动尝试读取上一级仓库根目录的 config.yaml（与主项目共用一份密钥）。
    """
    tav = cfg.setdefault("tavily", {})
    if str(tav.get("api_key") or "").strip():
        return
    resolved = loaded_path.resolve()
    if resolved.parent.name != "nanoAgent-langgraph":
        return
    root_cfg = resolved.parent.parent / "config.yaml"
    if not root_cfg.is_file():
        return
    try:
        root_doc = yaml.safe_load(root_cfg.read_text(encoding="utf-8")) or {}
        key = (root_doc.get("tavily") or {}).get("api_key") or ""
        if str(key).strip():
            tav["api_key"] = key
    except OSError:
        pass


def _inherit_llm_from_repo_root_if_empty(cfg: dict[str, Any], loaded_path: Path) -> None:
    """
    子项目 nanoAgent-langgraph 的 llm.api_key 为空时，
    尝试从上一级仓库根目录的 config.yaml 继承 llm 段（与主项目共用配置）。
    """
    llm = cfg.setdefault("llm", {})
    if str(llm.get("api_key") or "").strip():
        return
    resolved = loaded_path.resolve()
    if resolved.parent.name != "nanoAgent-langgraph":
        return
    root_cfg = resolved.parent.parent / "config.yaml"
    if not root_cfg.is_file():
        return
    try:
        root_doc = yaml.safe_load(root_cfg.read_text(encoding="utf-8")) or {}
        r_llm = root_doc.get("llm") or {}
        key = str(r_llm.get("api_key") or "").strip()
        if key:
            llm["api_key"] = r_llm.get("api_key")
        if not str(llm.get("base_url") or "").strip() and r_llm.get("base_url"):
            llm["base_url"] = r_llm["base_url"]
        if not str(llm.get("model") or "").strip() and r_llm.get("model"):
            llm["model"] = r_llm["model"]
    except OSError:
        pass


def ensure_llm_credentials(cfg: dict[str, Any]) -> None:
    """
    在调用 LLM 前校验密钥已配置；否则抛出带说明的 ValueError，避免 OpenAI 401 栈追踪。
    """
    llm = cfg.get("llm") or {}
    if str(llm.get("api_key") or "").strip():
        return
    raise ValueError(
        "未配置 LLM 访问密钥：请在 config.yaml 的 llm.api_key 中填写，或设置环境变量 OPENAI_API_KEY。\n"
        "若当前使用的是 config.yaml.example，请先复制为 config.yaml 并填入密钥；"
        "也可在仓库根目录配置 config.yaml 的 llm 段，子项目会自动尝试继承。"
    )


def load_config(path: str = "config.yaml") -> dict[str, Any]:
    """读取 YAML 配置并完成必要兜底。"""
    file_path, used_example = _resolve_config_file(path)
    if used_example:
        warnings.warn(
            f"未找到 {path}，已使用模板 {file_path.name}。"
            " 建议复制为 config.yaml 并填写密钥后再运行。",
            stacklevel=2,
        )
    content = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
    cfg = _merge_env(content)
    _inherit_tavily_from_repo_root_if_empty(cfg, file_path)
    _inherit_llm_from_repo_root_if_empty(cfg, file_path)
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
