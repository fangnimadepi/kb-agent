"""驱动 graph 并把节点更新转成事件流，处理人在回路审批的挂起/续跑。

审批流程（LangGraph interrupt + 持久化 checkpointer）：
  graph 跑到写操作 → interrupt 挂起，状态落 SQLite checkpoint（进程退出也不丢）
  → runner 检出挂起 → 用 approver 拿审批决策 → Command(resume=决策) 从断点续跑。
approver 决定"审批怎么发生"（命令行/自动/网页/飞书），图对此无感知。
"""

import uuid
from collections.abc import AsyncIterator

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from agent.approval import ApprovalRequest, Approver, ConsoleApprover
from agent.graph import build_graph
from agent.mcp_client import McpToolGateway

CHECKPOINT_DB = "data/agent_checkpoints.db"


async def run_agent(
    user_input: str,
    approver: Approver | None = None,
    thread_id: str | None = None,
) -> AsyncIterator[dict]:
    """执行一次 Agent 任务，逐个 yield trace 事件。写操作经 approver 审批。"""
    approver = approver or ConsoleApprover()
    thread_id = thread_id or uuid.uuid4().hex
    config = {"configurable": {"thread_id": thread_id}}

    async with (
        McpToolGateway() as gateway,
        AsyncSqliteSaver.from_conn_string(CHECKPOINT_DB) as checkpointer,
    ):
        graph = build_graph(gateway, checkpointer=checkpointer)
        cur_input: object = {
            "user_input": user_input,
            "messages": [],
            "trace": [],
            "tool_iters": 0,
            "reflect_retries": 0,
            "answer": "",
            "pending_writes": [],
        }
        seen = 0
        while True:
            async for state in graph.astream(cur_input, config, stream_mode="values"):
                trace = state.get("trace", [])
                for event in trace[seen:]:
                    yield event
                seen = len(trace)

            snap = await graph.aget_state(config)
            pending = _pending_interrupt(snap)
            if pending is None:
                return  # 图正常结束

            # 命中审批挂起：拿决策，从断点续跑
            req = ApprovalRequest(pending["tool"], pending["args"], pending["summary"])
            decision = await approver.decide(req)
            yield {
                "type": "approval_prompt",
                "summary": pending["summary"],
                "approved": decision.approved,
            }
            cur_input = Command(
                resume={
                    "approved": decision.approved,
                    "approver": decision.approver,
                    "comment": decision.comment,
                }
            )


def _pending_interrupt(snapshot) -> dict | None:
    """从状态快照里取出挂起的 interrupt 载荷（审批请求）；无挂起返回 None。"""
    for task in getattr(snapshot, "tasks", ()):
        for intr in getattr(task, "interrupts", ()):
            return intr.value
    return None
