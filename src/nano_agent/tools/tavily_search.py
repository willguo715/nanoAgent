from __future__ import annotations

from tavily import TavilyClient
from nano_agent.utils.retry import retry


class TavilySearchTool:
    """Tavily 搜索工具，返回精简结果用于后续总结。"""

    def __init__(self, api_key: str, max_results: int = 5) -> None:
        """初始化 Tavily 客户端与返回条数限制。"""
        self.client = TavilyClient(api_key=api_key)
        self.max_results = max_results

    @retry(attempts=3, min_wait_seconds=1, max_wait_seconds=4)
    def search(self, query: str) -> dict:
        """执行网络搜索并返回结构化结果。"""
        return self.client.search(query=query, max_results=self.max_results)
