"""生成真实感的合成工单数据集，让数据分析能力有东西可分析。

设计：约 400 条工单，覆盖全年（2026-01 ~ 07），带优先级/分类/负责人分布，
大部分已解决（带 resolved_at → 可算解决时长），少量在处理中/未分配。
解决时长按优先级分层：P0 快、P3 慢，符合真实运维直觉，让统计结果有意义。

用法：python scripts/seed_tickets.py [数量=400]
"""

import json
import random
import sys
import uuid
from datetime import datetime, timedelta

from servers.ticket_db import CATEGORIES, PRIORITIES, get_conn

random.seed(42)

ASSIGNEES = ["张伟", "李娜", "王芳", "刘强", "陈静", None]  # None = 未分配

# 每个分类的典型标题模板，让工单看起来像真的
TITLES = {
    "认证授权": [
        "登录后 token 频繁失效",
        "JWT 鉴权返回 401",
        "第三方 OAuth 回调失败",
        "验证码收不到",
    ],
    "文档入库": ["上传 PDF 卡在 parsing", "docx 解析乱码", "批量入库部分失败", "大文件入库超时"],
    "检索问答": ["检索结果不相关", "召回为空但库里有", "rerank 分数异常", "引用页码错位"],
    "计费账单": ["账单金额对不上", "余额扣费重复", "发票开具失败", "套餐升级未生效"],
    "部署运维": ["容器 OOM 重启", "worker 队列积压", "健康检查 502", "磁盘占满告警"],
    "权限管理": [
        "普通用户能看到他人知识库",
        "管理员无法分配角色",
        "组织架构同步失败",
        "越权访问漏洞",
    ],
    "其他": ["页面样式错乱", "导出功能报错", "接口文档过期", "功能建议：批量删除"],
}

# 优先级对应的解决时长范围（小时），P0 最快
RESOLVE_HOURS = {"P0": (1, 8), "P1": (4, 24), "P2": (12, 72), "P3": (24, 168)}
# 优先级抽样权重：P2 最多，P0 最少（真实分布）
PRIORITY_WEIGHTS = [0.08, 0.22, 0.45, 0.25]


def gen_one(created: datetime) -> tuple:
    category = random.choice(CATEGORIES)
    priority = random.choices(PRIORITIES, weights=PRIORITY_WEIGHTS)[0]
    title = random.choice(TITLES[category])
    reporter = f"user-{random.randint(1, 60):03d}"
    ticket_id = f"T-{uuid.uuid4().hex[:8]}"

    # 状态分布：70% 已闭环(resolved/closed)，20% 处理中，10% 待处理
    roll = random.random()
    assignee = random.choice(ASSIGNEES)
    resolved_at = None
    updated = created
    if roll < 0.70:
        status = random.choice(["resolved", "closed"])
        lo, hi = RESOLVE_HOURS[priority]
        resolved = created + timedelta(hours=random.uniform(lo, hi))
        resolved_at = resolved.isoformat(timespec="seconds")
        updated = resolved
        assignee = assignee or random.choice(ASSIGNEES[:-1])  # 已解决必有负责人
    elif roll < 0.90:
        status = "in_progress"
        updated = created + timedelta(hours=random.uniform(1, 48))
        assignee = assignee or random.choice(ASSIGNEES[:-1])
    else:
        status = "open"

    history = [{"at": created.isoformat(timespec="seconds"), "comment": "工单创建"}]
    if status != "open":
        history.append({"at": updated.isoformat(timespec="seconds"), "status": f"open -> {status}"})

    return (
        ticket_id,
        title,
        f"{title}。报告人反馈的问题，需排查。",
        reporter,
        assignee,
        category,
        priority,
        status,
        json.dumps(history, ensure_ascii=False),
        created.isoformat(timespec="seconds"),
        updated.isoformat(timespec="seconds"),
        resolved_at,
    )


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 400
    start = datetime(2026, 1, 1)
    span_days = 195  # 到 7 月中旬
    rows = []
    for _ in range(n):
        created = start + timedelta(days=random.uniform(0, span_days), hours=random.uniform(0, 24))
        rows.append(gen_one(created))

    with get_conn() as conn:
        conn.execute("DELETE FROM tickets")  # 重新灌，保证幂等
        conn.executemany(
            "INSERT INTO tickets (id,title,description,reporter,assignee,category,priority,"
            "status,history,created_at,updated_at,resolved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        total = conn.execute("SELECT COUNT(*) c FROM tickets").fetchone()["c"]
    print(f"seeded {total} tickets（2026-01 ~ 07，含各优先级/分类/状态分布）")


if __name__ == "__main__":
    main()
