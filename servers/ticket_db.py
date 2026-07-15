"""工单库的 schema 与连接助手，被 ticket server、种子脚本、统计工具共享。

设计要点：
- 字段支持分析——priority/category/assignee/resolved_at 让"按优先级统计、
  平均解决时长、按模块报障分布"这类问题可回答。
- 提供只读连接（mode=ro）：Text2SQL 工具用它做纵深防御，即便 SQL 校验被绕过，
  数据库层也拒绝任何写操作。
"""

import os
import sqlite3
from pathlib import Path

DB_PATH = os.environ.get("TICKET_DB_PATH", "data/tickets.db")

VALID_STATUS = ("open", "in_progress", "resolved", "closed")
PRIORITIES = ("P0", "P1", "P2", "P3")
CATEGORIES = ("认证授权", "文档入库", "检索问答", "计费账单", "部署运维", "权限管理", "其他")

# 合法状态迁移；closed 是终态
TRANSITIONS = {
    "open": {"in_progress", "resolved", "closed"},
    "in_progress": {"resolved", "closed"},
    "resolved": {"closed", "in_progress"},  # 用户不认可方案可重新打开
    "closed": set(),
}

_DDL = """
CREATE TABLE IF NOT EXISTS tickets (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    description TEXT NOT NULL,
    reporter    TEXT NOT NULL,
    assignee    TEXT,
    category    TEXT NOT NULL DEFAULT '其他',
    priority    TEXT NOT NULL DEFAULT 'P2',
    status      TEXT NOT NULL DEFAULT 'open',
    history     TEXT NOT NULL DEFAULT '[]',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    resolved_at TEXT
);
"""


def get_conn() -> sqlite3.Connection:
    """读写连接（建表幂等）。"""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    return conn


def get_readonly_conn() -> sqlite3.Connection:
    """只读连接：Text2SQL 执行 SQL 时用，数据库层面禁止一切写操作。"""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    if not Path(DB_PATH).exists():
        get_conn().close()  # 确保文件与表存在，否则 ro 打开会失败
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# 供 Agent 生成 SQL 时参考（嵌进工具描述）
SCHEMA_DOC = """表 tickets：
  id TEXT 主键（形如 T-xxxxxxxx）
  title TEXT 标题
  description TEXT 详情
  reporter TEXT 报告人
  assignee TEXT 负责人（可空 = 未分配）
  category TEXT 分类，取值：认证授权/文档入库/检索问答/计费账单/部署运维/权限管理/其他
  priority TEXT 优先级，取值：P0(最高)/P1/P2/P3
  status TEXT 状态，取值：open/in_progress/resolved/closed
  created_at TEXT 创建时间 ISO8601（如 2026-03-15T10:20:00）
  updated_at TEXT 最后更新时间
  resolved_at TEXT 解决时间（可空；未解决为 NULL）
说明：解决时长 = resolved_at - created_at，用 julianday() 计算天数。"""
