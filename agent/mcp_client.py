"""MCP 工具网关：同时连接三个 stdio server，把它们的工具聚合成一张表。

对 LangGraph 屏蔽 MCP 细节——编排层只看到"一批可调用工具"，
不关心某个工具属于哪个 server。这层是 Agent 与 MCP 生态的边界。

生命周期：async with McpToolGateway(...) as gw —— 进入时拉起三个子进程并
initialize，退出时统一关闭。会话期间复用，不为每次工具调用重开进程。
"""

import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent.config import settings


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    session: ClientSession  # 该工具所属 server 的会话


def _server_defs() -> dict[str, dict]:
    """server 名 -> {module, env}。env 只透传该 server 需要的变量。"""
    return {
        "kb-search": {
            "module": "servers.kb_search",
            "env": {
                "KB_API_BASE": settings.kb_api_base,
                "KB_ACCESS_TOKEN": settings.kb_access_token,
            },
        },
        "ticket": {
            "module": "servers.ticket",
            "env": {"TICKET_DB_PATH": settings.ticket_db_path},
        },
        "web-fetch": {"module": "servers.web_fetch", "env": {}},
    }


class McpToolGateway:
    def __init__(self) -> None:
        self._stack = AsyncExitStack()
        self.tools: dict[str, ToolSpec] = {}

    async def __aenter__(self) -> "McpToolGateway":
        import os

        for _name, cfg in _server_defs().items():
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", cfg["module"]],
                env={**os.environ, **cfg["env"]},
            )
            read, write = await self._stack.enter_async_context(stdio_client(params))
            session = await self._stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            listed = await session.list_tools()
            for t in listed.tools:
                self.tools[t.name] = ToolSpec(
                    name=t.name,
                    description=t.description or "",
                    input_schema=t.inputSchema,
                    session=session,
                )
        return self

    async def __aexit__(self, *exc) -> None:
        await self._stack.aclose()

    def openai_tools(self) -> list[dict]:
        """转成 OpenAI function-calling 的 tools 格式，供 LLM 选择调用。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                },
            }
            for t in self.tools.values()
        ]

    async def call(self, name: str, arguments: dict) -> str:
        """调用工具，返回文本结果（FastMCP 把 list 拆成多 content 块，这里拼回来）。"""
        spec = self.tools.get(name)
        if spec is None:
            return f'{{"error": "未知工具: {name}"}}'
        result = await spec.session.call_tool(name, arguments)
        parts = [c.text for c in result.content if hasattr(c, "text")]
        return "\n".join(parts) if len(parts) != 1 else parts[0]
