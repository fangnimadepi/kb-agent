"""短期记忆（token 裁剪）确定性测试。长期向量记忆需 embedding API，走集成脚本。"""

from agent.memory import trim_history


def sys_(c):
    return {"role": "system", "content": c}


def usr(c):
    return {"role": "user", "content": c}


def ai(c, tool_calls=None):
    m = {"role": "assistant", "content": c}
    if tool_calls:
        m["tool_calls"] = tool_calls
    return m


def tool(c):
    return {"role": "tool", "content": c, "tool_call_id": "x"}


def test_system_always_kept():
    msgs = [sys_("S"), *[usr("x" * 100) for _ in range(20)]]
    out = trim_history(msgs, budget=50)
    assert out[0]["role"] == "system"


def test_latest_kept_even_if_over_budget():
    out = trim_history([sys_("S"), usr("巨长" * 500)], budget=10)
    assert out[-1]["content"].startswith("巨长")


def test_drops_oldest_first():
    msgs = [sys_("S"), usr("q1"), ai("a1"), usr("q2"), ai("a2")]
    out = trim_history(msgs, budget=12)  # system(5) + 最新一条，容不下更多
    contents = [m["content"] for m in out]
    assert "a2" in contents and "q1" not in contents


def test_within_budget_keeps_all():
    msgs = [sys_("S"), usr("q1"), ai("a1")]
    assert len(trim_history(msgs, budget=100000)) == 3


def test_orphan_tool_message_dropped():
    # 如果裁剪后最前面是孤儿 tool（其 assistant 被裁掉），必须丢弃避免 LLM 报错
    msgs = [sys_("S"), ai("call", tool_calls=[{"id": "x"}]), tool("res" * 200), usr("q" * 5)]
    out = trim_history(msgs, budget=30)
    assert out[0]["role"] == "system"
    assert all(m["role"] != "tool" or i > 0 for i, m in enumerate(out))
    # 首条对话消息不应是 tool
    dialog = [m for m in out if m["role"] != "system"]
    assert not dialog or dialog[0]["role"] != "tool"
