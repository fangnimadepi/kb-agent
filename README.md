# KB-Agent 企业工单智能运维 Agent（MCP + LangGraph）

> 面向内部支持/运维团队的工单智能助手。三个核心能力：
> 1. **工单数据分析**——自然语言统计工单（结构化工具 + 只读 Text2SQL），如"今年各优先级平均解决时长"
> 2. **人在回路审批工作流**——创建/升级工单不直接执行，Agent 起草请求 → 挂起持久化 → 人工审批 → 断点续跑
> 3. **知识检索**——通过 MCP 复用 [KB-Copilot 平台](https://github.com/fangnimadepi/kb-copilot) 的两阶段检索
>
> Agent 与工具通过 **MCP（Model Context Protocol）标准化解耦**：工具是独立进程，可被任意 MCP 客户端复用。

## 架构

```
用户 ──> LangGraph Agent（plan 意图拆解 → act 工具循环 → approval 人工审批 → reflect 反思重试）
              │ MCP (stdio)
      ┌───────┼───────────────┬──────────────┐
   ticket           kb-search        web-fetch
   工单 CRUD + 状态机       封装 KB-Copilot     网页抓取
   + 统计 + 只读Text2SQL    两阶段检索 API      + 正文提取
   (SQLite)
```

## 三个 MCP Server（官方 python-sdk，可被任意 MCP 客户端复用）

| Server | 工具 | 说明 |
|---|---|---|
| `ticket` | create/get/list/update_ticket、ticket_stats、query_tickets_sql | 工单 CRUD + 状态机 + 结构化统计 + 只读 Text2SQL（双重防护） |
| `kb-search` | search_knowledge | 封装 KB-Copilot 两阶段检索 API（召回+rerank+页码溯源） |
| `web-fetch` | fetch_page | 网页抓取 + 正文提取摘要 |

## Roadmap

- [x] M1：3 个 MCP server + MCP 客户端验证
- [x] M2：LangGraph 编排（意图识别 → 拆解 → 工具调用 → 校验 → 反思重试 ≤2 次 + 兜底）
- [x] M3：工单数据分析（400 条合成工单 + 结构化统计 + 只读 Text2SQL 防注入）
- [ ] M4：人在回路审批工作流（LangGraph interrupt + checkpointer，创建/升级工单需人工签核）
- [ ] M5：记忆（短期窗口裁剪 + 长期向量记忆）
- [ ] M6：30 条多步任务评测（完成率 / 工具调用次数 / 审批拦截率）

## 快速开始

```bash
uv venv && uv pip install -e ".[agent,dev]"
cp .env.example .env   # 填 DEEPSEEK_API_KEY；KB_API_BASE/KB_ACCESS_TOKEN 指向 KB-Copilot
python scripts/seed_tickets.py 400          # 灌合成工单
python scripts/try_agent.py "统计今年各优先级工单数量和平均解决时长"
```

## 开发日志 / 技术决策

见 [docs/devlog.md](docs/devlog.md)。
