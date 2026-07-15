# 开发日志

> 每天记录：做了什么 / 踩了什么坑 / 做了什么决策。

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
