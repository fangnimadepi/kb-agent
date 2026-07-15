"""驱动 graph 并把节点更新转成事件流，处理人在回路审批的挂起/续跑。

审批流程（LangGraph interrupt + 持久化 checkpointer）：
  graph 跑到写操作 → interrupt 挂起，状态落 SQLite checkpoint（进程退出也不丢）
  → runner 检出挂起 → 用 approver 拿审批决策 → Command(resume=决策) 从断点续跑。
approver 决定"审批怎么发生"（命令行/自动/网页/飞书），图对此无感知。
"""

import asyncio
import uuid
from collections.abc import AsyncIterator

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from agent.approval import ApprovalRequest, Approver, ConsoleApprover
from agent.graph import build_graph
from agent.mcp_client import McpToolGateway
from agent.memory import MemoryStore

CHECKPOINT_DB = "data/agent_checkpoints.db"


async def run_agent(
    user_input: str,
    approver: Approver | None = None,
    thread_id: str | None = None,
    user_id: str = "demo用户",
    memory: MemoryStore | None = None,
) -> AsyncIterator[dict]:
    """执行一次 Agent 任务，逐个 yield trace 事件。写操作经 approver 审批。

    长期记忆：开跑前按当前问题召回该用户的历史记忆注入上下文；跑完提炼本轮记忆入库。
    传 memory=None 时禁用长期记忆（如未配 embedding key 的场景）。
    """
    approver = approver or ConsoleApprover()
    thread_id = thread_id or uuid.uuid4().hex
    config = {"configurable": {"thread_id": thread_id}}

    # 长期记忆召回（阻塞调用丢线程池，不卡事件循环）
    memory_context = ""
    if memory is not None:
        recalled = await asyncio.to_thread(memory.recall, user_id, user_input)
        if recalled:
            memory_context = "\n".join(f"- {m}" for m in recalled)
            yield {"type": "memory_recall", "content": memory_context}

    final_messages: list = []
    async with (
        McpToolGateway() as gateway,
        AsyncSqliteSaver.from_conn_string(CHECKPOINT_DB) as checkpointer,
    ):
        graph = build_graph(gateway, checkpointer=checkpointer)
        cur_input: object = {
            "user_input": user_input,
            "memory_context": memory_context,
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
                final_messages = state.get("messages", final_messages)
                trace = state.get("trace", [])
                for event in trace[seen:]:
                    yield event
                seen = len(trace)

            snap = await graph.aget_state(config)
            pending = _pending_interrupt(snap)
            if pending is None:
                break  # 图正常结束

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

    # 长期记忆写入：会话结束后提炼本轮的持久事实
    if memory is not None and final_messages:
        convo = _format_conversation(user_input, final_messages)
        facts = await asyncio.to_thread(memory.extract_and_store, user_id, convo)
        if facts:
            yield {"type": "memory_write", "content": facts}


def _format_conversation(user_input: str, messages: list) -> str:
    """把消息历史压成一段纯文本，供记忆提炼。只取用户问题和最终助手回答，
    跳过工具调用噪声。"""
    lines = [f"用户: {user_input}"]
    for m in messages:
        role = getattr(m, "type", None) or (m.get("role") if isinstance(m, dict) else "")
        content = getattr(m, "content", None) or (m.get("content") if isinstance(m, dict) else "")
        if role in ("ai", "assistant") and content and not getattr(m, "tool_calls", None):
            lines.append(f"助手: {content}")
    return "\n".join(lines[-8:])


def _pending_interrupt(snapshot) -> dict | None:
    """从状态快照里取出挂起的 interrupt 载荷（审批请求）；无挂起返回 None。"""
    for task in getattr(snapshot, "tasks", ()):
        for intr in getattr(task, "interrupts", ()):
            return intr.value
    return None
