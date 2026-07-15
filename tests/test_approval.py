"""审批逻辑的确定性测试（不经 LLM）：approver 策略 + 写操作识别 + 摘要生成。"""

import pytest

from agent.approval import ApprovalRequest, AutoApprover, ConsoleApprover
from agent.graph import WRITE_TOOLS, _write_summary


def req(tool="create_ticket", **args):
    return ApprovalRequest(tool=tool, args=args, summary="x")


async def test_auto_approve():
    d = await AutoApprover(approve=True).decide(req())
    assert d.approved and d.approver == "auto"


async def test_auto_reject():
    d = await AutoApprover(approve=False).decide(req())
    assert not d.approved and d.comment


async def test_hold_priority_intercepts():
    approver = AutoApprover(approve=True, hold_priorities=("P0", "P1"))
    assert not (await approver.decide(req(priority="P0"))).approved  # 高危被拦
    assert (await approver.decide(req(priority="P2"))).approved  # 普通放行


def test_write_tools_set():
    # 只有 create/update 是需审批的写操作；读工具不在内
    assert WRITE_TOOLS == {"create_ticket", "update_ticket"}
    for read_tool in ("search_knowledge", "ticket_stats", "query_tickets_sql", "get_ticket"):
        assert read_tool not in WRITE_TOOLS


@pytest.mark.parametrize(
    "tool,args,expect",
    [
        ("create_ticket", {"priority": "P1", "title": "检索报错"}, "创建工单：[P1] 检索报错"),
        (
            "update_ticket",
            {"ticket_id": "T-1", "status": "resolved"},
            "更新工单 T-1，状态→resolved",
        ),
        ("update_ticket", {"ticket_id": "T-2", "assignee": "张三"}, "更新工单 T-2，指派→张三"),
    ],
)
def test_write_summary(tool, args, expect):
    assert _write_summary(tool, args) == expect


def test_console_approver_is_approver():
    # 保证 ConsoleApprover 满足接口（有 decide 协程）
    assert hasattr(ConsoleApprover(), "decide")
