# 开发日志

> 每天记录：做了什么 / 踩了什么坑 / 做了什么决策。

## 2026-07-14（Day 1 · M1 完成：三个 MCP server）

**关键决策**
- **与 kb-copilot 分仓库**：两个置顶项目各自完整；更重要的是强化解耦叙事——Agent 不 import 平台任何代码，只通过 MCP 工具与平台 HTTP API 交互，平台换实现/换部署位置，Agent 侧只改环境变量
- **业务场景**：智能客服工单 Agent，服务对象是 KB-Copilot 平台自己的用户（dogfooding）——客服知识库用平台自身文档（README/FAQ/排错手册，全 Markdown，平台原生支持入库），工单场景闭环：报障 → 查工单 → 检索 FAQ 给方案 → 确认 → 更新工单
- 工单状态机显式建模（open→in_progress→resolved→closed，resolved 可退回），非法迁移返回 error 而非静默接受——给 Agent 的工具必须有防御性，Agent 会犯错

**做了什么**
- 三个 MCP server（官方 python-sdk FastMCP，stdio）：kb-search（封装平台 /api/search 两阶段检索）、ticket（SQLite 工单 CRUD + 状态机）、web-fetch（trafilatura 正文提取，截断 4000 字符防上下文爆炸）
- kb-copilot 侧新增 /api/search 纯检索接口（不走 LLM）并部署上线
- 验收脚本 scripts/verify_servers.py：MCP 客户端逐个拉起 server → list_tools → 实际调用。**kb-search 直接打线上平台验证**（本地 MCP server ↔ 远程平台），三个 server 全过

**踩坑**
- FastMCP 把 list 返回值序列化成多个 content 块（每元素一块），客户端读 content[0] 只拿到第一个元素——曾导致"8 ticket(s)"实为一个 dict 的 8 个键这种静默错误。工具返回结构的序列化行为必须实测，不能想当然

**面试考点自查**
- MCP vs Function Calling：标准化（任意 MCP 客户端可复用这三个 server，Claude Desktop/Inspector 都能直接挂）、解耦（工具进程独立于 Agent 进程）、跨模型（换 LLM 不用改工具层）
- tools/resources/prompts 三原语：本项目用 tools（模型主动调用）；resources 是应用侧注入的上下文（如把工单详情作为资源挂载）；prompts 是用户侧模板

**下一步（M2：LangGraph 编排）**
- 意图识别 → 任务拆解 → 工具调用循环 → 结果校验 → 反思重试（≤2 次）→ 兜底话术
- Agent 通过 MCP 客户端会话持有三个 server 的工具
