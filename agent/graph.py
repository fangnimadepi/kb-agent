"""LangGraph 编排：意图识别/拆解 → 工具调用循环 → 结果反思重试。

设计为可观测：每个节点往 state.trace 追加结构化事件（Annotated + operator.add
归并），外层用 graph.astream(stream_mode="updates") 逐节点消费，转成 SSE 推给前端，
实现"工具调用轨迹可视化"——这是 Agent 版的引用溯源。

控制流：
  plan → act ⇄（有 tool_calls 时循环，上限 max_tool_iterations）→ reflect
  reflect →（判定未解决且有重试额度时带反馈回 act）或 → END

反思重试（特赞 JD"错误兜底与重试机制"）：answer 产出后由一个校验 LLM 判断
是否真正回应了任务；不合格且有额度则把不合格原因作为反馈注入，回到 act 重来。
"""

import json
import operator
from typing import Annotated, TypedDict

from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt

from agent.config import settings
from agent.mcp_client import McpToolGateway
from agent.memory import trim_history

# 有副作用、需人工审批的写操作工具（读工具直接执行不拦）
WRITE_TOOLS = {"create_ticket", "update_ticket"}


def _write_summary(tool: str, args: dict) -> str:
    """把写操作翻译成一句人类可读的"将要发生什么"，给审批人看。"""
    if tool == "create_ticket":
        return f"创建工单：[{args.get('priority', 'P2')}] {args.get('title', '')}"
    if tool == "update_ticket":
        bits = [f"工单 {args.get('ticket_id', '')}"]
        if args.get("status"):
            bits.append(f"状态→{args['status']}")
        if args.get("assignee"):
            bits.append(f"指派→{args['assignee']}")
        return "更新" + "，".join(bits)
    return f"{tool} {args}"


_llm = ChatOpenAI(
    model=settings.llm_model,
    api_key=settings.deepseek_api_key,
    base_url=settings.deepseek_base_url,
    temperature=0.2,
    timeout=60,
)

SYSTEM = """你是"茅小龙"，贵州茅台投研知识库平台的智能助手。你能调用工具：
- search_knowledge：检索知识库（贵州茅台 2020–2025 年报/半年报），回答投研事实问题的首选
- create_ticket：创建工单。用户要"建工单/记录/跟踪"某事时，直接调用它，不要去检索"如何建工单"
- get_ticket / list_tickets / update_ticket：查询和更新已有工单
- fetch_page：抓取指定网页正文，获取知识库外的实时信息

工具选择铁律（避免绕路）：
- 要"建工单/记录/跟踪" → 直接调 create_ticket（description 里写清事由，reporter 未知就填"demo用户"）
- 要"查历史工单/看进展" → 调 list_tickets 或 get_ticket
- 要投研数据（营收/分红/利润等）→ 调 search_knowledge
- search_knowledge 返回空，说明知识库没有，别反复换词重试同一主题；如实告知即可

工作原则：
1. 事实结论必须来自工具结果，不编造；数字与原文一致
2. 一个请求含多个诉求（如"查数据 + 建工单"）时，逐个用对应工具完成，缺一不可
3. 所有工具都调用完、信息齐全后，再输出面向用户的最终回答"""

PLAN_PROMPT = """用户请求：{user_input}

请用 2~4 句话说明：① 用户的意图是什么；② 你打算分几步、用哪些工具完成。
只输出计划，不要执行。"""

REFLECT_PROMPT = """用户最初的请求：{user_input}

助手给出的回答：{answer}

请判断这个回答是否真正、完整地解决了用户的请求。只输出 JSON：
{{"ok": true}} 或 {{"ok": false, "reason": "还差什么/哪里不对"}}"""


class AgentState(TypedDict):
    user_input: str
    memory_context: str  # 长期记忆召回结果（runner 注入），空串表示无
    messages: Annotated[list, add_messages]  # 对话历史，跨节点累积（不是替换）
    trace: Annotated[list, operator.add]  # 可视化事件流
    tool_iters: int
    reflect_retries: int
    answer: str
    pending_writes: list  # 待审批的写操作工具调用队列


def build_graph(gateway: McpToolGateway, checkpointer=None):
    llm_with_tools = _llm.bind_tools(gateway.openai_tools())

    async def plan_node(state: AgentState) -> dict:
        resp = await _llm.ainvoke(
            [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": PLAN_PROMPT.format(user_input=state["user_input"])},
            ]
        )
        plan = resp.content
        # 长期记忆：有召回则拼进 system，让后续工具调用带上用户历史偏好
        system = SYSTEM
        mem = state.get("memory_context", "")
        trace = [{"type": "plan", "content": plan}]
        if mem:
            system = f"{SYSTEM}\n\n[关于当前用户的已知信息]\n{mem}"
            trace.insert(0, {"type": "memory_recall", "content": mem})
        return {
            "trace": trace,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": state["user_input"]},
                {"role": "assistant", "content": f"（内部计划）{plan}"},
                {"role": "user", "content": "请按计划执行，需要时调用工具。"},
            ],
        }

    async def act_node(state: AgentState) -> dict:
        # 短期记忆：对话过长时按 token 预算裁剪后再喂给 LLM
        resp = await llm_with_tools.ainvoke(
            trim_history(state["messages"], settings.context_token_budget)
        )
        new_messages = [resp]
        trace = []

        if not resp.tool_calls:
            return {
                "messages": new_messages,
                "answer": resp.content,
                "trace": [{"type": "answer", "content": resp.content}],
            }

        # 读工具立即执行；写工具入队等审批（此节点不 interrupt，避免副作用被重放）
        pending = []
        for tc in resp.tool_calls:
            if tc["name"] in WRITE_TOOLS:
                pending.append({"name": tc["name"], "args": tc["args"], "id": tc["id"]})
                continue
            trace.append({"type": "tool_call", "tool": tc["name"], "args": tc["args"]})
            result = await gateway.call(tc["name"], tc["args"])
            trace.append({"type": "tool_result", "tool": tc["name"], "result": result})
            new_messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
        return {
            "messages": new_messages,
            "trace": trace,
            "tool_iters": state["tool_iters"] + 1,
            "pending_writes": pending,
        }

    async def approval_node(state: AgentState) -> dict:
        """人在回路：对队首写操作发起审批并挂起。interrupt 之外不做任何事，
        保证 resume 时本节点重放无副作用。resume 传入 {approved, approver, comment}。"""
        wr = state["pending_writes"][0]
        summary = _write_summary(wr["name"], wr["args"])
        decision = interrupt(
            {"type": "approval_request", "tool": wr["name"], "args": wr["args"], "summary": summary}
        )
        return {"trace": [{"type": "approval_decision", "summary": summary, **decision}]}

    async def execute_write_node(state: AgentState) -> dict:
        """审批后执行或跳过队首写操作，并为其 tool_call_id 补上结果消息
        （否则下一轮 LLM 调用会因 tool_call 无响应而报错）。"""
        wr = state["pending_writes"][0]
        decision = state["trace"][-1]  # approval_node 刚写入的裁决
        if decision.get("approved"):
            trace = [{"type": "tool_call", "tool": wr["name"], "args": wr["args"]}]
            result = await gateway.call(wr["name"], wr["args"])
            trace.append({"type": "tool_result", "tool": wr["name"], "result": result})
        else:
            result = json.dumps(
                {"rejected": True, "reason": decision.get("comment", "审批未通过，未执行")},
                ensure_ascii=False,
            )
            trace = [
                {"type": "tool_rejected", "tool": wr["name"], "reason": decision.get("comment")}
            ]
        return {
            "messages": [{"role": "tool", "tool_call_id": wr["id"], "content": result}],
            "trace": trace,
            "pending_writes": state["pending_writes"][1:],
        }

    async def finalize_node(state: AgentState) -> dict:
        """工具循环撞上限却还没答案：强制不带工具生成最终回答，避免终态为空。"""
        resp = await _llm.ainvoke(
            state["messages"]
            + [
                {
                    "role": "user",
                    "content": "已达工具调用上限，请根据目前掌握的信息直接给出最终回答。",
                }
            ]
        )
        return {"answer": resp.content, "trace": [{"type": "answer", "content": resp.content}]}

    async def reflect_node(state: AgentState) -> dict:
        resp = await _llm.ainvoke(
            [
                {
                    "role": "user",
                    "content": REFLECT_PROMPT.format(
                        user_input=state["user_input"], answer=state["answer"]
                    ),
                }
            ]
        )
        try:
            verdict = json.loads(resp.content)
        except (json.JSONDecodeError, TypeError):
            verdict = {"ok": True}  # 校验器故障时不阻塞，放行答案

        if verdict.get("ok"):
            return {"trace": [{"type": "reflect", "ok": True}]}

        reason = verdict.get("reason", "回答不完整")
        # 重试额度耗尽：不再清空 answer，保留当前最佳答案作为终态（避免答案丢失）
        if state["reflect_retries"] >= settings.max_reflect_retries:
            return {
                "reflect_retries": state["reflect_retries"] + 1,
                "trace": [
                    {"type": "reflect", "ok": False, "reason": reason, "exhausted": True},
                    {"type": "answer", "content": state["answer"], "final": True},
                ],
            }
        return {
            "trace": [{"type": "reflect", "ok": False, "reason": reason}],
            "reflect_retries": state["reflect_retries"] + 1,
            "answer": "",  # 清空，否则 route_after_act 会绕过重新执行直接回到 reflect
            "messages": [
                {"role": "user", "content": f"上一版回答存在问题：{reason}。请修正后重新回答。"}
            ],
        }

    def route_after_act(state: AgentState) -> str:
        # 有写操作待审批 → 先过审批闸；否则按原逻辑走反思/兜底/继续
        if state.get("pending_writes"):
            return "approval"
        if state.get("answer"):
            return "reflect"
        if state["tool_iters"] >= settings.max_tool_iterations:
            return "finalize"
        return "act"

    def route_after_execute(state: AgentState) -> str:
        # 还有排队的写操作 → 回审批处理下一个；否则回 act 让 LLM 看到工具结果继续
        return "approval" if state.get("pending_writes") else "act"

    def route_after_reflect(state: AgentState) -> str:
        last_reflect = next(e for e in reversed(state["trace"]) if e["type"] == "reflect")
        if last_reflect.get("ok") or last_reflect.get("exhausted"):
            return END
        return "act"

    g = StateGraph(AgentState)
    g.add_node("plan", plan_node)
    g.add_node("act", act_node)
    g.add_node("approval", approval_node)
    g.add_node("execute_write", execute_write_node)
    g.add_node("finalize", finalize_node)
    g.add_node("reflect", reflect_node)
    g.set_entry_point("plan")
    g.add_edge("plan", "act")
    g.add_conditional_edges(
        "act",
        route_after_act,
        {"approval": "approval", "act": "act", "reflect": "reflect", "finalize": "finalize"},
    )
    g.add_edge("approval", "execute_write")
    g.add_conditional_edges(
        "execute_write", route_after_execute, {"approval": "approval", "act": "act"}
    )
    g.add_edge("finalize", "reflect")
    g.add_conditional_edges("reflect", route_after_reflect, {"act": "act", END: END})
    return g.compile(checkpointer=checkpointer)
