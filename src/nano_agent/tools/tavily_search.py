from __future__ import annotations

from tavily import TavilyClient
from nano_agent.utils.retry import retry


class TavilySearchTool:
    """Tavily 搜索工具，返回精简结果用于后续总结。"""

    def __init__(self, api_key: str, max_results: int = 5) -> None:
        """保存密钥与条数；无密钥时不创建客户端，避免 Agent 启动即失败。"""
        self._api_key = (api_key or "").strip()
        self._client: TavilyClient | None = None
        self.max_results = max_results

    def _ensure_client(self) -> TavilyClient | None:
        """仅在配置了 API key 时懒加载 TavilyClient。"""
        if not self._api_key:
            return None
        if self._client is None:
            self._client = TavilyClient(api_key=self._api_key)
        return self._client

    @retry(attempts=3, min_wait_seconds=1, max_wait_seconds=4)
    def search(self, query: str) -> dict:
        """执行网络搜索并返回结构化结果。"""
        client = self._ensure_client()
        if client is None:
            return {
                "ok": False,
                "error": "未配置 Tavily：请在 config.yaml 的 tavily.api_key 或环境变量 TAVILY_API_KEY 中填写密钥。",
            }
        return client.search(query=query, max_results=self.max_results)
