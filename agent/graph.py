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

from agent.config import settings
from agent.mcp_client import McpToolGateway

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
    messages: list  # 传给 LLM 的对话（含工具结果）
    trace: Annotated[list, operator.add]  # 可视化事件流
    tool_iters: int
    reflect_retries: int
    answer: str


def build_graph(gateway: McpToolGateway):
    llm_with_tools = _llm.bind_tools(gateway.openai_tools())

    async def plan_node(state: AgentState) -> dict:
        resp = await _llm.ainvoke(
            [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": PLAN_PROMPT.format(user_input=state["user_input"])},
            ]
        )
        plan = resp.content
        return {
            "trace": [{"type": "plan", "content": plan}],
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": state["user_input"]},
                {"role": "assistant", "content": f"（内部计划）{plan}"},
                {"role": "user", "content": "请按计划执行，需要时调用工具。"},
            ],
        }

    async def act_node(state: AgentState) -> dict:
        resp = await llm_with_tools.ainvoke(state["messages"])
        new_messages = [resp]
        trace = []

        if not resp.tool_calls:
            return {
                "messages": new_messages,
                "answer": resp.content,
                "trace": [{"type": "answer", "content": resp.content}],
            }

        for tc in resp.tool_calls:
            trace.append({"type": "tool_call", "tool": tc["name"], "args": tc["args"]})
            result = await gateway.call(tc["name"], tc["args"])
            trace.append({"type": "tool_result", "tool": tc["name"], "result": result})
            new_messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
        return {"messages": new_messages, "trace": trace, "tool_iters": state["tool_iters"] + 1}

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
        # 有答案（无工具调用）→ 去反思；撞工具上限但没答案 → finalize 强制出答案
        if state.get("answer"):
            return "reflect"
        if state["tool_iters"] >= settings.max_tool_iterations:
            return "finalize"
        return "act"

    def route_after_reflect(state: AgentState) -> str:
        last_reflect = next(e for e in reversed(state["trace"]) if e["type"] == "reflect")
        if last_reflect.get("ok") or last_reflect.get("exhausted"):
            return END
        return "act"

    g = StateGraph(AgentState)
    g.add_node("plan", plan_node)
    g.add_node("act", act_node)
    g.add_node("finalize", finalize_node)
    g.add_node("reflect", reflect_node)
    g.set_entry_point("plan")
    g.add_edge("plan", "act")
    g.add_conditional_edges(
        "act", route_after_act, {"act": "act", "reflect": "reflect", "finalize": "finalize"}
    )
    g.add_edge("finalize", "reflect")
    g.add_conditional_edges("reflect", route_after_reflect, {"act": "act", END: END})
    return g.compile()
