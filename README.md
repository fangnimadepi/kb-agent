# KB-Agent 智能客服工单 Agent（MCP + LangGraph）

> 为 [KB-Copilot 知识库平台](https://github.com/fangnimadepi/kb-copilot) 配套的智能客服 Agent：
> 用户报障 → Agent 查工单 → 检索平台知识库给出解决方案 → 确认后自动更新工单。
> 平台与 Agent 通过 **MCP（Model Context Protocol）标准化解耦**——Agent 不直接依赖平台代码，
> 只通过 MCP 工具与平台交互。

## 架构

```
用户 ── SSE ──> LangGraph Agent（意图识别 → 任务拆解 → 工具调用 → 结果校验 → 反思重试）
                    │ MCP (stdio)
        ┌───────────┼───────────────┐
   kb-search      ticket        web-fetch
   （封装 KB-Copilot │（工单 CRUD，  │（网页抓取
    检索 API）      │ SQLite）      │ + 正文提取）
```

## 三个 MCP Server（官方 python-sdk，可被任意 MCP 客户端复用）

| Server | 工具 | 说明 |
|---|---|---|
| `kb-search` | search_knowledge | 封装 KB-Copilot 两阶段检索 API（召回+rerank+页码溯源） |
| `ticket` | create/get/list/update_ticket | 工单系统（SQLite 模拟业务系统） |
| `web-fetch` | fetch_page | 网页抓取 + 正文提取摘要 |

## Roadmap

- [x] M1：3 个 MCP server + MCP 客户端验证
- [x] M2：LangGraph 编排（意图识别 → 拆解 → 工具调用 → 校验 → 反思重试 ≤2 次 + 兜底）
- [ ] M3：记忆（短期窗口裁剪 + 长期向量记忆）
- [ ] M4：30 条多步任务评测（完成率 / 平均工具调用次数 / 幻觉率）
- [ ] M5：部署 + 接入 KB-Copilot 演示页 Agent 标签页

## 开发日志 / 技术决策

见 [docs/devlog.md](docs/devlog.md) 与 [docs/adr/](docs/adr/)。
