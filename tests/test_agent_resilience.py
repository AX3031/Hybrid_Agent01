"""Agent 循环韧性用例。

对应验收：
- 工具异常不再打断整轮对话，而是变成 status="error" 的 ToolMessage；
- 只对幂等工具、且只对瞬时错误做有限重试；
- 上一次中断留下的悬空 tool_call 能被修复，后续对话可继续。
"""

import unittest

import requests
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from tests.fakes import ScriptedToolModel


def _make_graph(tool_impl, model: ScriptedToolModel, middleware=None):
    """用假模型 + 指定中间件组装一张最小图。"""
    graph = create_agent(
        model,
        tools=[tool_impl],
        middleware=middleware or [],
        checkpointer=InMemorySaver(),
    )
    return graph, {"configurable": {"thread_id": "test"}}


def _run(graph, config, text: str):
    return list(graph.stream({"messages": [HumanMessage(text)]}, config=config))


def _tool_messages(graph, config) -> list[ToolMessage]:
    state = graph.get_state(config)
    return [m for m in state.values.get("messages", []) if isinstance(m, ToolMessage)]


class ToolErrorMiddlewareTest(unittest.TestCase):
    def test_exception_becomes_error_tool_message_and_loop_continues(self):
        from agent.tools.middleware import tool_error_middleware

        @tool
        def boom() -> str:
            """总是失败。"""
            raise RuntimeError("模拟工具崩溃")

        model = ScriptedToolModel(tool_name="boom", final_text="已知悉失败，继续处理")
        graph, config = _make_graph(boom, model, [tool_error_middleware])

        chunks = _run(graph, config, "执行任务")

        self.assertTrue(chunks, "整轮应当正常结束，不应该抛异常")
        final = graph.get_state(config).values["messages"][-1]
        self.assertIsInstance(final, AIMessage)
        self.assertIn("已知悉失败", final.content)

        errors = [m for m in _tool_messages(graph, config) if m.status == "error"]
        self.assertEqual(len(errors), 1, "异常应转成一条 status=error 的工具消息")
        self.assertIn("boom", errors[0].content)


class ToolRetryMiddlewareTest(unittest.TestCase):
    def test_transient_error_retried_then_success(self):
        from agent.tools.middleware import tool_error_middleware, tool_retry_middleware

        attempts = {"n": 0}

        @tool("web_search_baidu")
        def flaky_search(query: str) -> str:
            """前两次连接失败，第三次成功。"""
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise requests.exceptions.ConnectionError("模拟连接中断")
            return "搜索结果"

        model = ScriptedToolModel(tool_name="web_search_baidu", tool_args={"query": "x"})
        graph, config = _make_graph(flaky_search, model, [tool_error_middleware, tool_retry_middleware])

        _run(graph, config, "搜一下")

        self.assertEqual(attempts["n"], 3, "瞬时错误应重试到成功为止")
        results = _tool_messages(graph, config)
        self.assertEqual(results[-1].status, "success")
        self.assertIn("搜索结果", results[-1].content)

    def test_auth_error_not_retried(self):
        from agent.tools.middleware import tool_error_middleware, tool_retry_middleware

        attempts = {"n": 0}

        @tool("web_search_baidu")
        def unauthorized(query: str) -> str:
            """鉴权失败：重试没有意义。"""
            attempts["n"] += 1
            response = requests.Response()
            response.status_code = 401
            raise requests.exceptions.HTTPError("401 Client Error", response=response)

        model = ScriptedToolModel(tool_name="web_search_baidu", tool_args={"query": "x"})
        graph, config = _make_graph(unauthorized, model, [tool_error_middleware, tool_retry_middleware])

        _run(graph, config, "搜一下")

        self.assertEqual(attempts["n"], 1, "401 不应重试")
        self.assertEqual(_tool_messages(graph, config)[-1].status, "error")

    def test_side_effect_tool_not_retried(self):
        from agent.tools.middleware import tool_error_middleware, tool_retry_middleware

        attempts = {"n": 0}

        @tool("write_text_file")
        def risky_write(path: str, content: str) -> str:
            """非幂等工具，即使抛网络异常也不该重试。"""
            attempts["n"] += 1
            raise requests.exceptions.ConnectionError("模拟连接中断")

        model = ScriptedToolModel(
            tool_name="write_text_file", tool_args={"path": "a.txt", "content": "x"}
        )
        graph, config = _make_graph(risky_write, model, [tool_error_middleware, tool_retry_middleware])

        _run(graph, config, "写文件")

        self.assertEqual(attempts["n"], 1, "写文件不重试，避免副作用放大")


class DanglingToolCallRepairTest(unittest.TestCase):
    def test_unanswered_tool_call_is_detected_and_repaired(self):
        from agent.resilience import find_unanswered_tool_calls, repair_thread_state

        @tool
        def boom() -> str:
            """总是失败。"""
            raise RuntimeError("模拟工具崩溃")

        model = ScriptedToolModel(tool_name="boom", tool_turns=1, final_text="第二轮正常完成")
        graph, config = _make_graph(boom, model)

        with self.assertRaises(RuntimeError):
            _run(graph, config, "第一轮")

        pending = find_unanswered_tool_calls(graph.get_state(config).values["messages"])
        self.assertEqual(len(pending), 1, "失败后应能识别出悬空的 tool_call")

        repaired = repair_thread_state(graph, config)
        self.assertEqual(len(repaired), 1)
        self.assertEqual(find_unanswered_tool_calls(graph.get_state(config).values["messages"]), [])

        # 修复后同一 thread 应能正常跑完第二轮
        graph.invoke({"messages": [HumanMessage("第二轮")]}, config=config)
        self.assertEqual(graph.get_state(config).values["messages"][-1].content, "第二轮正常完成")

    def test_noop_when_no_dangling_call(self):
        from agent.resilience import repair_thread_state

        @tool
        def ok_tool() -> str:
            """正常返回。"""
            return "ok"

        model = ScriptedToolModel(tool_name="ok_tool", final_text="好了")
        graph, config = _make_graph(ok_tool, model)
        _run(graph, config, "跑一下")

        self.assertEqual(repair_thread_state(graph, config), [])


if __name__ == "__main__":
    unittest.main()
