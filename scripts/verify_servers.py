"""M1 验收：用 MCP 官方客户端逐个验证三个 server。

对每个 server：stdio 拉起 → initialize → list_tools → 实际调用工具验证行为。
kb-search 直接打线上平台（体现解耦：本地 MCP server ↔ 远程平台 API）。

用法：python scripts/verify_servers.py
"""

import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run_server(module: str, env: dict, checks) -> None:
    params = StdioServerParameters(
        command=sys.executable, args=["-m", module], env={**os.environ, **env}
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print(f"\n=== {module} tools: {names}")
            await checks(session)


def _payload(result) -> dict | list:
    return json.loads(result.content[0].text)


def _payload_list(result) -> list:
    """FastMCP 把 list 返回值拆成多个 content 块（每元素一块），需全量解析。"""
    return [json.loads(c.text) for c in result.content]


async def check_ticket(s: ClientSession) -> None:
    r = _payload(
        await s.call_tool(
            "create_ticket",
            {
                "title": "文档入库任务卡在 parsing",
                "description": "上传 PDF 后任务停在 parsing 5% 超过 10 分钟",
                "reporter": "user-001",
            },
        )
    )
    tid = r["id"]
    print("create:", r)
    assert r["status"] == "open"

    r = _payload(
        await s.call_tool(
            "update_ticket",
            {
                "ticket_id": tid,
                "status": "in_progress",
                "comment": "已检索到 FAQ：worker 崩溃重投场景",
            },
        )
    )
    assert r["status"] == "in_progress"

    # 非法迁移必须被拒绝
    r = _payload(await s.call_tool("update_ticket", {"ticket_id": tid, "status": "open"}))
    assert "error" in r, r
    print("illegal transition rejected:", r["error"])

    r = _payload(await s.call_tool("get_ticket", {"ticket_id": tid}))
    assert len(r["history"]) == 1
    r = _payload_list(await s.call_tool("list_tickets", {"reporter": "user-001"}))
    assert all("id" in t for t in r)
    print(f"list: {len(r)} ticket(s)")
    print("ticket OK")


async def check_web_fetch(s: ClientSession) -> None:
    r = _payload(await s.call_tool("fetch_page", {"url": "https://example.com"}))
    assert "content" in r and len(r["content"]) > 20, r
    print(f"fetched: title={r['title']!r}, {len(r['content'])} chars")
    r = _payload(await s.call_tool("fetch_page", {"url": "https://invalid.invalid/x"}))
    assert "error" in r
    print("error path OK:", r["error"][:60])
    print("web-fetch OK")


async def check_kb_search(s: ClientSession) -> None:
    r = _payload_list(
        await s.call_tool("search_knowledge", {"query": "2024年营业总收入", "top_k": 3})
    )
    assert r, "检索结果为空"
    top = r[0]
    print(f"top1: {top['filename']} p{top['page_start']} score={top['rerank_score']}")
    assert top["rerank_score"] > 0.35
    print("kb-search OK")


async def main() -> None:
    await run_server("servers.ticket", {"TICKET_DB_PATH": "data/verify_tickets.db"}, check_ticket)
    await run_server("servers.web_fetch", {}, check_web_fetch)
    await run_server(
        "servers.kb_search",
        {
            "KB_API_BASE": os.environ.get("KB_API_BASE", "http://xiaoloong.miyaki.top:3389"),
            "KB_ACCESS_TOKEN": os.environ.get("KB_ACCESS_TOKEN", ""),
        },
        check_kb_search,
    )
    print("\nALL MCP SERVERS VERIFIED")


if __name__ == "__main__":
    asyncio.run(main())
