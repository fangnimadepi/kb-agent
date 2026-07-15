"""演示跨会话长期记忆：会话1 让 Agent 记住用户，会话2 验证召回。

用法：python scripts/try_memory.py
"""

import asyncio
import sys

sys.path.insert(0, ".")

from agent.approval import AutoApprover
from agent.memory import MemoryStore
from agent.runner import run_agent

USER = "user-张工"


async def session(label, question, memory):
    print(f"\n{'=' * 70}\n[{label}] 用户({USER}): {question}")
    async for ev in run_agent(
        question, approver=AutoApprover(approve=True), user_id=USER, memory=memory
    ):
        t = ev["type"]
        if t == "memory_recall":
            print(f"  🧠 召回记忆:\n{ev['content']}")
        elif t == "memory_write":
            print(f"  💾 写入记忆: {ev['content']}")
        elif t == "tool_call":
            print(f"  🔧 {ev['tool']} {ev['args']}")
        elif t == "answer" and ev.get("content"):
            print(f"  ✅ {ev['content'][:100]}")


async def main():
    memory = MemoryStore()
    # 清空该用户旧记忆，保证演示干净
    memory.db.execute("DELETE FROM memories WHERE user_id = ?", (USER,))
    memory.db.commit()

    # 会话1：用户透露身份和关注点
    await session(
        "会话1",
        "我是负责文档入库模块的张工，最近老是遇到 PDF 入库卡在 parsing 的问题，帮我建个工单跟踪，P1",
        memory,
    )

    # 会话2：新会话，只问一句模糊的话，看能否召回身份/模块
    await session("会话2（新会话）", "我之前关心的那个问题，有类似的历史工单吗？", memory)

    print(f"\n{'=' * 70}\n该用户已存记忆：")
    for (text,) in memory.db.execute(
        "SELECT text FROM memories WHERE user_id = ?", (USER,)
    ).fetchall():
        print(f"  - {text}")
    memory.close()


asyncio.run(main())
