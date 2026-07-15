"""工单状态机的确定性测试（不经 MCP 传输，直连底层逻辑）。"""

import os
import tempfile

import pytest

os.environ["TICKET_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")

from servers import ticket  # noqa: E402

# 本版 FastMCP 的 @mcp.tool() 原样返回函数，直接调用
create = ticket.create_ticket
get = ticket.get_ticket
update = ticket.update_ticket
list_ = ticket.list_tickets
stats = ticket.ticket_stats
sql = ticket.query_tickets_sql


def test_create_and_get():
    t = create("标题", "描述", "u1")
    assert t["status"] == "open" and t["id"].startswith("T-")
    got = get(t["id"])
    assert got["title"] == "标题" and got["history"] == []


def test_legal_transition_and_history():
    t = create("x", "y", "u2")
    r = update(t["id"], status="in_progress", comment="处理中")
    assert r["status"] == "in_progress"
    r = update(t["id"], status="resolved", comment="给出方案")
    assert r["status"] == "resolved"
    assert get(t["id"])["history"] and len(get(t["id"])["history"]) == 2


def test_illegal_transition_rejected():
    t = create("x", "y", "u3")
    update(t["id"], status="resolved")
    r = update(t["id"], status="open")  # resolved 不可回 open
    assert "error" in r
    assert get(t["id"])["status"] == "resolved"  # 状态未被非法改动


def test_closed_is_terminal():
    t = create("x", "y", "u4")
    update(t["id"], status="closed")
    r = update(t["id"], status="in_progress")
    assert "error" in r


def test_unknown_ticket():
    assert "error" in update("T-nope", status="closed")
    assert "error" in get("T-nope")


def test_list_filters():
    create("a", "b", "reporterX")
    assert all(t["reporter"] == "reporterX" for t in list_(reporter="reporterX"))


def test_invalid_status_value():
    t = create("x", "y", "u5")
    assert "error" in update(t["id"], status="flying")


@pytest.mark.parametrize(
    "frm,to,ok",
    [
        ("open", "in_progress", True),
        ("open", "closed", True),
        ("in_progress", "resolved", True),
        ("resolved", "in_progress", True),
        ("closed", "open", False),
        ("resolved", "open", False),
    ],
)
def test_transition_table(frm, to, ok):
    assert (to in ticket.TRANSITIONS[frm]) == ok


def test_resolved_at_recorded():
    t = create("x", "y", "u6", priority="P0")
    update(t["id"], status="resolved", comment="done")
    assert get(t["id"])["resolved_at"] is not None


def test_stats_shape():
    s = stats()
    assert {"total", "by_status", "by_priority", "by_category", "avg_resolution_hours"} <= s.keys()
    assert s["total"] == sum(s["by_status"].values())


def test_sql_select_works():
    r = sql("SELECT COUNT(*) AS n FROM tickets")
    assert r["columns"] == ["n"] and r["rows"][0][0] >= 0


@pytest.mark.parametrize(
    "bad",
    [
        "DELETE FROM tickets",
        "DROP TABLE tickets",
        "UPDATE tickets SET status='closed'",
        "INSERT INTO tickets VALUES (1)",
        "SELECT 1; DELETE FROM tickets",
        "SELECT * FROM tickets; DROP TABLE tickets",
    ],
)
def test_sql_write_rejected(bad):
    assert "error" in sql(bad)


def test_sql_write_leaves_data_intact():
    before = sql("SELECT COUNT(*) FROM tickets")["rows"][0][0]
    sql("DELETE FROM tickets")  # 被拦
    after = sql("SELECT COUNT(*) FROM tickets")["rows"][0][0]
    assert before == after
