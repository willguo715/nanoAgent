from __future__ import annotations

import argparse
import sys

from nano_agent_crewai.config import ensure_llm_credentials, load_config
from nano_agent_crewai.core.crew_agent import CrewAIAgentRunner


def _clean_image_path(image: str | None) -> str | None:
    """清洗用户粘贴路径中常见的双引号包裹。"""
    if not image:
        return None
    return image.strip().strip('"').strip("'")


def run_command(args: argparse.Namespace) -> None:
    """执行单次任务：加载配置、运行一轮 agent 并打印报告。"""
    cfg = load_config(args.config)
    try:
        ensure_llm_credentials(cfg)
    except ValueError as exc:
        print(f"[nano-agent-crewai] 配置错误：{exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    runner = CrewAIAgentRunner(cfg, config_path=args.config)
    print("[nano-agent-crewai] 任务开始...")
    report = runner.run_once(
        session_name=args.session,
        user_input=args.prompt,
        image_path=_clean_image_path(args.image),
        progress_cb=lambda msg: print(f"[progress] {msg}"),
    )
    print("[nano-agent-crewai] 任务结束，以下是最终报告：\n")
    print(report)


def chat_command(args: argparse.Namespace) -> None:
    """启动交互式多轮会话，直到用户输入退出命令。"""
    cfg = load_config(args.config)
    try:
        ensure_llm_credentials(cfg)
    except ValueError as exc:
        print(f"[nano-agent-crewai] 配置错误：{exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    runner = CrewAIAgentRunner(cfg, config_path=args.config)
    print(f"[nano-agent-crewai] chat started, session={args.session}, 输入 exit 退出。")
    while True:
        prompt = input("\n你> ").strip()
        if prompt.lower() in {"exit", "quit"}:
            print("[nano-agent-crewai] bye.")
            break
        image = input("图片路径(可留空)> ").strip()
        image = _clean_image_path(image or None)
        print("[nano-agent-crewai] 开始处理...")
        report = runner.run_once(
            session_name=args.session,
            user_input=prompt,
            image_path=image,
            progress_cb=lambda msg: print(f"[progress] {msg}"),
        )
        print("[nano-agent-crewai] 处理完成。")
        print("\n" + report + "\n")


def build_parser() -> argparse.ArgumentParser:
    """构建 CLI 参数解析器并绑定子命令处理函数。"""
    parser = argparse.ArgumentParser(
        prog="nano-agent-crewai",
        description="基于 CrewAI 的本地 CLI agent（规划用 Crew，报告与主项目 LLMTool 对齐）。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="单次执行")
    run_p.add_argument("--config", default="config.yaml", help="配置文件路径")
    run_p.add_argument("prompt", help="用户输入文本")
    run_p.add_argument("--image", default=None, help="本地图片路径")
    run_p.add_argument("--session", default="default", help="会话名")
    run_p.set_defaults(func=run_command)

    chat_p = sub.add_parser("chat", help="进入多轮对话")
    chat_p.add_argument("--config", default="config.yaml", help="配置文件路径")
    chat_p.add_argument("--session", default="default", help="会话名")
    chat_p.set_defaults(func=chat_command)
    return parser


def main() -> None:
    """程序入口：解析命令行参数并分发到对应子命令。"""
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
