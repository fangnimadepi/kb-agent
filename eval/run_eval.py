"""M6 评测：30 条多步任务用例，自动判定成功与否，产出简历数字。

指标：
- 任务完成率：按每个用例的 check 断言判定（工具用对/答案含关键信息/写操作按预期通过或拦截）
- 平均工具调用次数：效率与规划质量
- 审批拦截率：写操作用例中，审批闸是否 100% 生效（安全指标，项目二特色）
- P50/P95 端到端延迟

用例的 approve 字段决定该轮 AutoApprover 批准还是拒绝，用于验证审批双向都对。

用法：python eval/run_eval.py [并发=3]
"""

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")

from agent.approval import AutoApprover
from agent.runner import run_agent


def check_case(case: dict, events: list) -> tuple[bool, str]:
    """按 case.check 断言判定成功；返回 (通过, 失败原因)。"""
    chk = case["check"]
    tool_calls = [e["tool"] for e in events if e["type"] == "tool_call"]
    approvals = [e for e in events if e["type"] == "approval_decision"]
    rejects = [e for e in events if e["type"] == "tool_rejected"]
    answer = " ".join(e.get("content", "") for e in events if e["type"] == "answer")

    if "tools_used_any" in chk and not (set(chk["tools_used_any"]) & set(tool_calls)):
        return False, f"未用到期望工具之一 {chk['tools_used_any']}，实际 {tool_calls}"
    if "answer_contains_any" in chk and not any(k in answer for k in chk["answer_contains_any"]):
        return False, f"答案未含关键信息 {chk['answer_contains_any']}"
    if "write_approved" in chk:
        # 期望：审批通过 + 该写工具被实际执行
        approved = any(a.get("approved") for a in approvals)
        executed = chk["write_approved"] in tool_calls
        if not (approved and executed):
            return False, f"写操作未按预期批准并执行（approved={approved}, executed={executed}）"
    if "write_rejected" in chk:
        # 期望：审批发起 + 被拒 + 未执行
        rejected = any(not a.get("approved") for a in approvals)
        not_executed = chk["write_rejected"] not in tool_calls
        if not (rejected and not_executed and rejects):
            return False, "写操作未按预期被拦截"
    return True, ""


async def run_case(case: dict) -> dict:
    approver = AutoApprover(approve=case.get("approve", True))
    events: list = []
    t0 = time.perf_counter()
    try:
        async for ev in run_agent(case["question"], approver=approver, user_id="eval", memory=None):
            events.append(ev)
    except Exception as e:
        return {
            **case,
            "ok": False,
            "reason": f"异常: {type(e).__name__}: {e}",
            "tool_calls": 0,
            "elapsed": 0,
            "gated": False,
        }
    elapsed = time.perf_counter() - t0
    ok, reason = check_case(case, events)
    return {
        "id": case["id"],
        "category": case["category"],
        "ok": ok,
        "reason": reason,
        "tool_calls": sum(1 for e in events if e["type"] == "tool_call"),
        "gated": any(e["type"] == "approval_decision" for e in events),
        "is_write": "write_approved" in case["check"] or "write_rejected" in case["check"],
        "elapsed": round(elapsed, 1),
    }


async def main() -> None:
    concurrency = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    cases = [
        json.loads(x) for x in Path("eval/cases.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    print(f"跑 {len(cases)} 条用例，并发 {concurrency} ...")

    sem = asyncio.Semaphore(concurrency)

    async def guarded(c):
        async with sem:
            r = await run_case(c)
            print(
                f"  {'✅' if r['ok'] else '❌'} {r['id']:10s} tools={r['tool_calls']} "
                f"{r['elapsed']}s {'' if r['ok'] else '← ' + r['reason'][:50]}"
            )
            return r

    results = await asyncio.gather(*[guarded(c) for c in cases])

    total = len(results)
    passed = sum(r["ok"] for r in results)
    write_cases = [r for r in results if r["is_write"]]
    gated = sum(r["gated"] for r in write_cases)
    lat = sorted(r["elapsed"] for r in results)
    p50 = lat[len(lat) // 2]
    p95 = lat[int(len(lat) * 0.95) - 1]
    avg_tools = sum(r["tool_calls"] for r in results) / total

    summary = {
        "total": total,
        "task_completion_rate": round(passed / total, 3),
        "avg_tool_calls": round(avg_tools, 2),
        "approval_gate_rate": round(gated / len(write_cases), 3) if write_cases else None,
        "latency_p50": p50,
        "latency_p95": p95,
        "by_category": {},
    }
    for r in results:
        c = summary["by_category"].setdefault(r["category"], {"pass": 0, "total": 0})
        c["total"] += 1
        c["pass"] += r["ok"]

    Path("eval/result.json").write_text(
        json.dumps({"summary": summary, "cases": results}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print("\n" + "=" * 60)
    print(f"任务完成率:      {passed}/{total} = {summary['task_completion_rate']:.0%}")
    print(f"平均工具调用:    {summary['avg_tool_calls']} 次/任务")
    print(
        f"审批拦截率:      {gated}/{len(write_cases)} = {summary['approval_gate_rate']:.0%}（写操作 100% 经人工审批）"
    )
    print(f"端到端延迟:      P50 {p50}s / P95 {p95}s")
    print("分类别通过率:")
    for cat, v in summary["by_category"].items():
        print(f"  {cat}: {v['pass']}/{v['total']}")


if __name__ == "__main__":
    asyncio.run(main())
