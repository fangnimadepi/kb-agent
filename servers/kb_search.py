"""MCP server: kb-search —— 封装 KB-Copilot 平台的两阶段检索 API。

Agent 与平台的唯一交互通道：不 import 平台任何代码，只走 HTTP。
平台换实现、换部署位置，这里只需改环境变量——这就是 MCP 解耦的意义。

运行：python -m servers.kb_search（stdio 传输）
"""

import os

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("kb-search")

KB_API_BASE = os.environ.get("KB_API_BASE", "http://127.0.0.1:8000")
KB_ACCESS_TOKEN = os.environ.get("KB_ACCESS_TOKEN", "")


@mcp.tool()
async def search_knowledge(query: str, top_k: int = 5) -> list[dict]:
    """检索 KB-Copilot 知识库（向量召回 + 重排序两阶段检索）。

    返回最相关的知识片段列表，每条含 content（正文）、filename（来源文件）、
    page_start/page_end（页码，用于引用溯源）、rerank_score（相关性 0~1，
    低于 0.35 的结果已被平台过滤；返回空列表表示知识库中没有相关内容）。

    Args:
        query: 检索问题，应自包含、具体（例如"文档入库任务失败如何重试"）
        top_k: 返回条数，默认 5，最大 20
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{KB_API_BASE}/api/search",
            json={"query": query, "top_k": top_k},
            headers={"X-Access-Token": KB_ACCESS_TOKEN},
        )
        resp.raise_for_status()
        return resp.json()["results"]


if __name__ == "__main__":
    mcp.run()
