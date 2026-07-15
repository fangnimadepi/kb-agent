# 开发日志

> 每天记录：做了什么 / 踩了什么坑 / 做了什么决策。

## 2026-07-15（Day 2 · M4 完成：人在回路审批工作流）★ 项目核心卖点

**做了什么**
- 审批渠道抽象（agent/approval.py）：Approver 接口 + AutoApprover（策略自动裁决，供评测/测试）+ ConsoleApprover（命令行 y/n）。飞书等真集成即扩展一个 Approver，图不动
- 图加审批闸（agent/graph.py）：写操作（create/update_ticket）不直接执行——act 把写工具入队 pending_writes → approval 节点 LangGraph interrupt 挂起 → execute_write 按裁决执行或跳过。读工具（检索/统计/SQL）不拦，直接执行
- 持久化：AsyncSqliteSaver checkpointer，interrupt 时状态落 data/agent_checkpoints.db（实测 44 条 checkpoint 落盘）——进程退出也能从断点续跑，这是"AI 发起→人工签核→自动续跑"完整故事的基础
- runner 改为 interrupt/resume 循环：stream 到挂起 → aget_state 取审批请求 → approver 决策 → Command(resume=决策) 续跑
- 端到端三场景全过：纯读零审批 / 写操作批准后执行 / 写操作拒绝后跳过不执行。33 单测全过

**踩坑（都是硬核）**
- **messages 字段没配 reducer = 静默数据损坏**：M2 起 AgentState.messages 是普通 list，每个节点返回的 messages **替换**整个历史而非追加。M2 单节点侥幸能跑，但审批流多节点追加（act 加助手消息+读结果、execute_write 补写结果）立刻暴露——LLM 收到孤立的 tool 消息，报"tool message must follow tool_calls"。修法：Annotated[list, add_messages]。教训：LangGraph 里凡是要累积的字段必须配 reducer，否则是替换
- **interrupt 节点会重放**：LangGraph 从断点 resume 时，含 interrupt 的节点会从头重新执行。所以 approval 节点里只放 interrupt、不做任何副作用；真正执行写操作放在独立的 execute_write 节点（不 interrupt，不重放）。读工具也不能放在会 interrupt 的节点里
- **tool_call_id 契约**：一个助手消息里的每个 tool_call 都必须有对应的 tool 结果消息，下一次 LLM 调用才不报错。所以写操作被拒绝时也要补一条"已拒绝"的 tool 结果消息，不能只是跳过

**面试考点（这块最值钱）**
- 为什么审批要 human-in-the-loop 而非 AI 自主：企业里有副作用的操作（改工单状态、扣费、发消息）需要审计和签核，Agent 全自动是合规风险。这是 Agent 落地企业的关键门槛
- LangGraph interrupt + checkpointer 怎么实现"挂起-续跑"：状态持久化到 SQLite，interrupt 抛出请求，外部拿到审批后 Command(resume) 从断点继续，进程崩了也不丢
- 为什么审批渠道要抽象：解耦"审批逻辑"与"审批渠道"，命令行/网页/飞书只是不同 Approver，符合开闭原则

**下一步（M5：记忆）**
- 短期：对话窗口按 token 裁剪（复用项目一思路）；长期：对话摘要存向量库，跨会话召回用户偏好

## 2026-07-15（Day 2 下半场 · 项目重新定位 + M3 数据分析能力）

**项目重新定位（重要）**
- 放弃"茅台知识库↔工单"的硬凑。定位改为**企业工单智能运维 Agent**：服务内部支持/运维团队，三个真本事——工单数据分析、人在回路审批工作流、知识检索（kb-search 保留为工具）。
- 约束确认：仅本地跑 + 推 GitHub，不部署服务器；演示不重要，目标丰富简历 → 力气花在能讲深的能力上。
- 两个决策：审批=可插拔模式（interrupt + pluggable approver，飞书留可选适配器不做真集成）；数据分析=混合（结构化统计工具 + 只读 Text2SQL）。

**M3 做了什么**
- 工单库 schema 升级（servers/ticket_db.py 共享）：加 priority/category/assignee/resolved_at，支撑"按优先级统计、平均解决时长、按模块分布"
- 种子脚本（scripts/seed_tickets.py）：400 条真实感合成工单，2026-01~07，状态/优先级/分类分布合理，解决时长按优先级分层（P0 快 P3 慢）
- 三类工具：CRUD+状态机（保留）、ticket_stats（结构化概览快照）、query_tickets_sql（只读 Text2SQL）
- 只读 Text2SQL 双重防护：① 语句校验（仅单条 SELECT/WITH、拦写关键字、拦分号多语句）② 数据库只读连接（mode=ro，纵深防御）。6 种注入/写攻击全部拦截、数据完好
- 端到端：Agent 面对"统计各优先级数量+平均解决时长+哪个模块报障最多"，自己写出正确 SQLite（julianday/strftime/CASE WHEN/GROUP BY），产出完整运营周报，reflect ok=True。P0 平均 5.7h / P3 90.7h 梯度清晰
- 25 单测全过

**踩坑**
- **FastMCP 装饰时固化工具描述**：想把 schema 动态拼进 query_tickets_sql 的描述，事后改 `__doc__` 不生效（M1 的描述是装饰时从 docstring 读的）。解法：`@mcp.tool(description=...)` 显式传参，def-time 就把 SCHEMA_DOC 拼进去。这样 Agent 的 LLM 才能通过 list_tools 看到表结构、正确生成 SQL
- Text2SQL 的 schema 必须让模型看到——把表结构写进工具描述是关键，否则模型瞎猜列名

**面试考点**
- Text2SQL 的安全：为什么"LLM 生成 SQL"必须配只读校验 + 只读连接双层防护（LLM 会被 prompt 注入诱导写危险 SQL）；结构化工具 vs Text2SQL 的取舍（常见问题走结构化省 token 更稳，长尾问题走 SQL 更灵活）
- MCP server 不该持有 LLM key：Text2SQL 的"NL→SQL"智能放在 Agent 侧，server 只做"校验+执行"，职责清晰

**下一步（M4：人在回路审批工作流）★ 核心卖点**
- LangGraph interrupt + checkpointer：创建/升级工单前挂起 → 发审批 → 人工批准 → 断点续跑

## 2026-07-15（Day 2 · M2 完成：LangGraph 编排）

**做了什么**
- MCP 工具网关（agent/mcp_client.py）：async with 同时拉起三个 stdio server、聚合成一张工具表，对 LangGraph 屏蔽 MCP 细节；会话期复用子进程不重开
- LangGraph 编排（agent/graph.py）：plan（意图识别+拆解）→ act（工具调用循环）→ reflect（校验 LLM 判定是否真正解决）→ 重试/finalize/END。trace 用 Annotated[list, operator.add] 归并，astream(stream_mode="values") 增量吐事件——为前端"工具调用轨迹可视化"铺路
- 端到端验证："查茅台2023分红 + 建跟踪工单"一句话串起 search_knowledge + create_ticket + update_ticket，反思发现首答未展示数据→重试→最终给出完整分红表（308.76元/10股，数字与语料一致），reflect ok=True 正常收尾
- 15 个确定性单测（工单状态机全覆盖 + 图可编译），不依赖 LLM

**踩坑 / 决策**
- **DeepSeek 工具选择弱**：想"建工单"却去 search_knowledge 查"如何建工单"——弱模型的经典 tool-selection 失败。修法：system prompt 加"工具选择铁律"（要建工单直接调 create_ticket，别检索怎么建）。这是给弱模型做 Agent 的必修课，Claude 不需要这么啰嗦
- **反思重试的两个坑**：① 重试回 act 前必须清空 answer，否则 route_after_act 直接跳回 reflect 空转；② 重试额度耗尽时若继续清空 answer，终态答案丢失——改为耗尽时保留当前最佳答案 + 加 finalize 节点（撞工具上限时强制无工具出答案）
- 反思是双刃剑：能自我纠错（首答漏数据→补全），但对弱模型也会放大混乱（一直判 not ok）。max_reflect_retries=2 是平衡点
- 场景微调：客服工单 → 投研服务工单（"查数据+建跟踪工单"），复用现有茅台语料，无需另造 FAQ 文档，叙事反而更顺（平台管知识，Agent 把知识变成动作）
- FastMCP 本版 @mcp.tool() 原样返回函数（无 .fn 包装），单测直接调

**面试考点**
- 为什么 plan/act/reflect 分节点而非一个大 prompt：可观测（每步可视化）、可控（各节点独立限流/重试）、可测（路由函数纯函数可单测）
- ReAct vs Plan-and-Execute：本项目是混合——先 plan 定框架，再 ReAct 式 act 循环，末尾 reflect 校验

**下一步（M3：记忆）**
- 短期：对话窗口按 token 裁剪（复用项目一思路）；长期：对话摘要存向量库，新会话召回用户历史偏好

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
