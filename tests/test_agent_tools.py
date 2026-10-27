"""业务工具的失败语义用例：失败必须可见，不能被吞成空字符串。"""

import os
import unittest
from unittest import mock

import requests

from agent.tools.agent_tools import web_search_baidu
from agent.tools.middleware import _should_retry_tool_error


class WebSearchFailureTest(unittest.TestCase):
    def test_missing_api_key_raises_readable_error(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("API_KEY_BAIDU", None)
            with self.assertRaises(RuntimeError) as ctx:
                web_search_baidu.invoke({"query": "哈基米"})

        self.assertIn("API_KEY_BAIDU", str(ctx.exception))

    def test_http_error_is_not_swallowed(self):
        response = requests.Response()
        response.status_code = 401
        with mock.patch.dict(os.environ, {"API_KEY_BAIDU": "dummy"}, clear=False), mock.patch(
            "agent.tools.agent_tools.requests.post", side_effect=requests.exceptions.HTTPError(
                "401 Client Error", response=response
            )
        ):
            with self.assertRaises(requests.exceptions.HTTPError):
                web_search_baidu.invoke({"query": "哈基米"})

    def test_no_result_returns_readable_note(self):
        with mock.patch.dict(os.environ, {"API_KEY_BAIDU": "dummy"}, clear=False), mock.patch(
            "agent.tools.agent_tools.requests.post"
        ) as post:
            post.return_value.json.return_value = {"references": []}
            post.return_value.raise_for_status.return_value = None
            text = web_search_baidu.invoke({"query": "哈基米"})

        self.assertIn("没有返回", text)
        self.assertNotEqual(text, "")


class RetryClassificationTest(unittest.TestCase):
    def _http_error(self, status_code: int) -> requests.exceptions.HTTPError:
        response = requests.Response()
        response.status_code = status_code
        return requests.exceptions.HTTPError(f"{status_code}", response=response)

    def test_transient_errors_are_retryable(self):
        self.assertTrue(_should_retry_tool_error(requests.exceptions.Timeout("超时")))
        self.assertTrue(_should_retry_tool_error(requests.exceptions.ConnectionError("断连")))
        self.assertTrue(_should_retry_tool_error(self._http_error(429)))
        self.assertTrue(_should_retry_tool_error(self._http_error(503)))

    def test_permanent_errors_are_not_retryable(self):
        self.assertFalse(_should_retry_tool_error(self._http_error(401)))
        self.assertFalse(_should_retry_tool_error(self._http_error(403)))
        self.assertFalse(_should_retry_tool_error(self._http_error(404)))
        self.assertFalse(_should_retry_tool_error(ValueError("参数不合法")))
        self.assertFalse(_should_retry_tool_error(FileNotFoundError("不存在")))


if __name__ == "__main__":
    unittest.main()
