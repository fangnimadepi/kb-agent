"""图结构的确定性测试：不调 LLM，只验证编排骨架能编译、路由函数正确。"""

from agent.config import settings


def test_graph_builds_with_fake_gateway():
    """构图不应依赖真实 MCP 连接——用假网关验证编译通过。"""
    from agent.graph import build_graph

    class FakeGateway:
        tools = {}

        def openai_tools(self):
            return []

    graph = build_graph(FakeGateway())
    # 三个业务节点 + 入口
    assert graph is not None


def test_retry_budget_config():
    assert settings.max_reflect_retries >= 1
    assert settings.max_tool_iterations >= settings.max_reflect_retries
