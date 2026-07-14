"""MCP server: web-fetch —— 网页抓取 + 正文提取。

用 trafilatura 提取正文（去导航/广告/脚本），截断到合理长度，
避免把整页 HTML 灌进 LLM 上下文。

运行：python -m servers.web_fetch（stdio 传输）
"""

import httpx
import trafilatura
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("web-fetch")

_MAX_CHARS = 4000
_UA = "Mozilla/5.0 (compatible; kb-agent/0.1; +https://github.com/fangnimadepi/kb-agent)"


@mcp.tool()
async def fetch_page(url: str) -> dict:
    """抓取网页并提取正文。

    返回 {url, title, content}；content 为提取后的正文纯文本（最长 4000 字符）。
    抓取失败或页面无正文时返回 {"error": ...}。

    Args:
        url: 完整 URL（含 http/https）
    """
    try:
        async with httpx.AsyncClient(
            timeout=20, follow_redirects=True, headers={"User-Agent": _UA}
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except httpx.HTTPError as e:
        return {"error": f"抓取失败: {type(e).__name__}: {e}"}

    content = trafilatura.extract(resp.text, include_comments=False, include_tables=True)
    if not content:
        return {"error": "页面无可提取正文（可能是纯 JS 渲染页面）"}
    meta = trafilatura.extract_metadata(resp.text)
    return {
        "url": str(resp.url),
        "title": (meta.title if meta else "") or "",
        "content": content[:_MAX_CHARS],
    }


if __name__ == "__main__":
    mcp.run()
