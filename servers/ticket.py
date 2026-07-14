"""MCP server: ticket —— 工单系统（SQLite 模拟业务系统）。

工单状态机：open → in_progress → resolved → closed（任意状态可 closed）。
每次状态变更/备注都追加到 history，保证处理过程可追溯。

运行：python -m servers.ticket（stdio 传输）
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("ticket")

DB_PATH = os.environ.get("TICKET_DB_PATH", "data/tickets.db")

VALID_STATUS = ("open", "in_progress", "resolved", "closed")
# 合法状态迁移；closed 是终态
TRANSITIONS = {
    "open": {"in_progress", "resolved", "closed"},
    "in_progress": {"resolved", "closed"},
    "resolved": {"closed", "in_progress"},  # 用户不认可方案可重新打开
    "closed": set(),
}


def _conn() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tickets (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            reporter TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            history TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["history"] = json.loads(d["history"])
    return d


@mcp.tool()
def create_ticket(title: str, description: str, reporter: str) -> dict:
    """创建一个新工单，返回工单完整信息（含 id）。

    Args:
        title: 一句话问题概述
        description: 问题详情（现象、报错信息、已尝试的操作）
        reporter: 报告人标识（用户名或会话 id）
    """
    now = datetime.now().isoformat(timespec="seconds")
    ticket_id = f"T-{uuid.uuid4().hex[:8]}"
    with _conn() as conn:
        conn.execute(
            "INSERT INTO tickets VALUES (?, ?, ?, ?, 'open', '[]', ?, ?)",
            (ticket_id, title, description, reporter, now, now),
        )
    return {"id": ticket_id, "title": title, "status": "open", "created_at": now}


@mcp.tool()
def get_ticket(ticket_id: str) -> dict:
    """按 id 查询工单详情（含状态、处理历史）。找不到时返回 {"error": ...}。"""
    with _conn() as conn:
        row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    return _row_to_dict(row) if row else {"error": f"工单不存在: {ticket_id}"}


@mcp.tool()
def list_tickets(reporter: str = "", status: str = "") -> list[dict]:
    """查询工单列表，可按报告人和/或状态过滤，按更新时间倒序。

    Args:
        reporter: 报告人标识，留空则不过滤
        status: open / in_progress / resolved / closed，留空则不过滤
    """
    sql, args = "SELECT * FROM tickets WHERE 1=1", []
    if reporter:
        sql += " AND reporter = ?"
        args.append(reporter)
    if status:
        sql += " AND status = ?"
        args.append(status)
    sql += " ORDER BY updated_at DESC LIMIT 50"
    with _conn() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_row_to_dict(r) for r in rows]


@mcp.tool()
def update_ticket(ticket_id: str, status: str = "", comment: str = "") -> dict:
    """更新工单：变更状态和/或追加处理备注。

    状态机约束：open→in_progress→resolved→closed；resolved 可退回 in_progress；
    closed 为终态不可再改。非法迁移会返回 error 而不是静默接受。

    Args:
        ticket_id: 工单 id
        status: 新状态（open/in_progress/resolved/closed），留空表示只加备注
        comment: 处理备注（例如给出的解决方案摘要）
    """
    if status and status not in VALID_STATUS:
        return {"error": f"非法状态: {status}（可选 {'/'.join(VALID_STATUS)}）"}
    with _conn() as conn:
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
        if comment:
            entry["comment"] = comment
        history.append(entry)
        conn.execute(
            "UPDATE tickets SET status = ?, history = ?, updated_at = ? WHERE id = ?",
            (status or current, json.dumps(history, ensure_ascii=False), now, ticket_id),
        )
        return {"id": ticket_id, "status": status or current, "history_len": len(history)}


if __name__ == "__main__":
    mcp.run()
