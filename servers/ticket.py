"""MCP server: ticket —— 工单系统（SQLite）。

三类工具：
1. CRUD + 状态机：create/get/list/update，状态迁移受 TRANSITIONS 约束
2. 结构化统计：ticket_stats 一次给出按状态/优先级/分类的分布 + 平均解决时长
3. 只读 Text2SQL：query_tickets_sql 执行 Agent 生成的 SELECT，双重防护
   （SQL 校验 + 只读连接），支持开放式数据分析问题

运行：python -m servers.ticket（stdio 传输）
"""

import json
import re
import uuid
from datetime import datetime

from mcp.server.fastmcp import FastMCP

from servers.ticket_db import (
    CATEGORIES,
    PRIORITIES,
    SCHEMA_DOC,
    TRANSITIONS,
    VALID_STATUS,
    get_conn,
    get_readonly_conn,
)

mcp = FastMCP("ticket")


def _row_to_dict(row) -> dict:
    d = dict(row)
    if "history" in d:
        d["history"] = json.loads(d["history"])
    return d


@mcp.tool()
def create_ticket(
    title: str,
    description: str,
    reporter: str,
    category: str = "其他",
    priority: str = "P2",
) -> dict:
    """创建一个新工单，返回工单信息（含 id）。

    Args:
        title: 一句话问题概述
        description: 问题详情（现象、报错信息、已尝试的操作）
        reporter: 报告人标识
        category: 分类，取值：认证授权/文档入库/检索问答/计费账单/部署运维/权限管理/其他
        priority: 优先级 P0(最高)/P1/P2/P3
    """
    if category not in CATEGORIES:
        category = "其他"
    if priority not in PRIORITIES:
        priority = "P2"
    now = datetime.now().isoformat(timespec="seconds")
    ticket_id = f"T-{uuid.uuid4().hex[:8]}"
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO tickets (id,title,description,reporter,category,priority,"
            "status,history,created_at,updated_at) VALUES (?,?,?,?,?,?, 'open','[]',?,?)",
            (ticket_id, title, description, reporter, category, priority, now, now),
        )
    return {"id": ticket_id, "title": title, "priority": priority, "status": "open"}


@mcp.tool()
def get_ticket(ticket_id: str) -> dict:
    """按 id 查询工单详情（含状态、处理历史）。找不到返回 {"error": ...}。"""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    return _row_to_dict(row) if row else {"error": f"工单不存在: {ticket_id}"}


@mcp.tool()
def list_tickets(reporter: str = "", status: str = "", priority: str = "") -> list[dict]:
    """查询工单列表，可按报告人/状态/优先级过滤，按更新时间倒序，最多 50 条。"""
    sql, args = "SELECT * FROM tickets WHERE 1=1", []
    for col, val in (("reporter", reporter), ("status", status), ("priority", priority)):
        if val:
            sql += f" AND {col} = ?"
            args.append(val)
    sql += " ORDER BY updated_at DESC LIMIT 50"
    with get_conn() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_row_to_dict(r) for r in rows]


@mcp.tool()
def update_ticket(ticket_id: str, status: str = "", comment: str = "", assignee: str = "") -> dict:
    """更新工单：变更状态、指派负责人、追加备注。

    状态机约束：open→in_progress→resolved→closed；resolved 可退回 in_progress；
    closed 为终态。非法迁移返回 error。变更为 resolved 时自动记录 resolved_at。
    """
    if status and status not in VALID_STATUS:
        return {"error": f"非法状态: {status}（可选 {'/'.join(VALID_STATUS)}）"}
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if row is None:
            return {"error": f"工单不存在: {ticket_id}"}
        current = row["status"]
        if status and status != current and status not in TRANSITIONS[current]:
            return {"error": f"非法状态迁移: {current} -> {status}"}

        now = datetime.now().isoformat(timespec="seconds")
        history = json.loads(row["history"])
        entry = {"at": now}
        if status and status != current:
            entry["status"] = f"{current} -> {status}"
        if assignee:
            entry["assignee"] = assignee
        if comment:
            entry["comment"] = comment
        history.append(entry)
        resolved_at = row["resolved_at"] or (now if status == "resolved" else None)
        conn.execute(
            "UPDATE tickets SET status=?, assignee=COALESCE(NULLIF(?,''),assignee), "
            "history=?, updated_at=?, resolved_at=? WHERE id=?",
            (
                status or current,
                assignee,
                json.dumps(history, ensure_ascii=False),
                now,
                resolved_at,
                ticket_id,
            ),
        )
        return {"id": ticket_id, "status": status or current, "history_len": len(history)}


@mcp.tool()
def ticket_stats() -> dict:
    """工单总体统计快照：总数、按状态/优先级/分类的分布、已解决工单的平均解决时长（小时）。

    回答"工单整体情况如何""各优先级多少""哪个模块报障最多"这类概览问题的首选，
    比自己写 SQL 更省。需要更细的自定义分析时再用 query_tickets_sql。
    """
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM tickets").fetchone()["c"]

        def dist(col: str) -> dict:
            rows = conn.execute(
                f"SELECT {col} k, COUNT(*) c FROM tickets GROUP BY {col}"
            ).fetchall()
            return {r["k"]: r["c"] for r in rows}

        avg_row = conn.execute(
            "SELECT AVG(julianday(resolved_at)-julianday(created_at))*24 h "
            "FROM tickets WHERE resolved_at IS NOT NULL"
        ).fetchone()
    avg_hours = round(avg_row["h"], 1) if avg_row["h"] is not None else None
    return {
        "total": total,
        "by_status": dist("status"),
        "by_priority": dist("priority"),
        "by_category": dist("category"),
        "avg_resolution_hours": avg_hours,
    }


# 只允许单条 SELECT/WITH；拦截任何写/DDL/多语句/注入常见手法
_WRITE_KEYWORDS = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|"
    r"vacuum|reindex|truncate)\b",
    re.IGNORECASE,
)


@mcp.tool(
    description=(
        "对工单库执行一条只读 SQL（SELECT）并返回结果，用于结构化统计工具覆盖不到的"
        "自定义分析。只读双重防护：语句校验（仅允许单条 SELECT/WITH）+ 数据库只读连接。\n\n"
        "可用表结构：\n" + SCHEMA_DOC + "\n\n"
        "参数 sql：一条 SELECT 语句（不要分号拼接多条、不要任何写操作）。"
    )
)
def query_tickets_sql(sql: str) -> dict:
    """执行只读 SQL 查询工单库（描述含 schema，见 @mcp.tool description）。"""
    stripped = sql.strip().rstrip(";").strip()
    if ";" in stripped:
        return {"error": "只允许单条语句，不要用分号拼接多条"}
    if not re.match(r"^(select|with)\b", stripped, re.IGNORECASE):
        return {"error": "只允许 SELECT/WITH 查询"}
    if _WRITE_KEYWORDS.search(stripped):
        return {"error": "检测到写操作关键字，已拒绝"}
    try:
        with get_readonly_conn() as conn:
            cur = conn.execute(stripped)
            columns = [d[0] for d in cur.description]
            rows = cur.fetchmany(200)
            return {"columns": columns, "rows": [list(r) for r in rows], "row_count": len(rows)}
    except Exception as e:
        return {"error": f"SQL 执行失败: {type(e).__name__}: {e}"}


if __name__ == "__main__":
    mcp.run()
