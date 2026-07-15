"""驱动 graph 并把节点更新转成事件流。供 CLI 与后续 SSE API 复用。"""

from collections.abc import AsyncIterator

from agent.graph import build_graph
from agent.mcp_client import McpToolGateway


async def run_agent(user_input: str) -> AsyncIterator[dict]:
    """执行一次 Agent 任务，逐个 yield trace 事件（plan/tool_call/tool_result/reflect/answer）。

    每次调用独占一个 MCP 网关（三个 server 子进程），任务结束即回收。
    """
    async with McpToolGateway() as gateway:
        graph = build_graph(gateway)
        init: dict = {
            "user_input": user_input,
            "messages": [],
            "trace": [],
            "tool_iters": 0,
            "reflect_retries": 0,
            "answer": "",
        }
        seen = 0
        async for update in graph.astream(init, stream_mode="values"):
            trace = update.get("trace", [])
            # values 模式给全量 state，增量地吐出新 trace
            for event in trace[seen:]:
                yield event
            seen = len(trace)
