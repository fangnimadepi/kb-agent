"""命令行跑一轮 Agent，打印完整轨迹。用法：python scripts/try_agent.py "你的问题" """

import asyncio
import sys

from agent.runner import run_agent

ICON = {
    "plan": "🧭 计划",
    "tool_call": "🔧 调用",
    "tool_result": "📥 结果",
    "reflect": "🔎 反思",
    "answer": "✅ 回答",
}


async def main() -> None:
    q = (
        sys.argv[1]
        if len(sys.argv) > 1
        else "我要报障：上传 PDF 后入库任务一直卡在 parsing 不动，帮我建个工单"
    )
    print(f"\n用户: {q}\n" + "=" * 70)
    async for ev in run_agent(q):
        t = ev["type"]
        if t == "tool_call":
            print(f"{ICON[t]}: {ev['tool']}  args={ev['args']}")
        elif t == "tool_result":
            r = ev["result"]
            print(f"{ICON[t]}: {ev['tool']} -> {r[:160]}{'…' if len(r) > 160 else ''}")
        elif t == "reflect":
            print(
                f"{ICON[t]}: ok={ev['ok']}"
                + (f"  reason={ev.get('reason')}" if not ev["ok"] else "")
            )
        else:
            print(f"{ICON[t]}: {ev['content']}")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
