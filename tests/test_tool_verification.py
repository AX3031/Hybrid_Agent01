"""结果验证（确定性后置校验）用例。

验证的两层意思：
1. 工具“说自己成功了”时，真的去文件系统核对后置条件；
2. 核对不上时，把状态改成 error，让模型和界面都能看到，而不是静默相信。
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from langchain_core.messages import ToolMessage

from agent.tools.os_tools import delete_path, move_path, write_text_file
from agent.tools.verification import enrich_tool_message, verify_tool_outcome
from utils.path_tool import get_project_root


def _fake_tool_message(content: str, status: str = "success") -> ToolMessage:
    return ToolMessage(content=content, tool_call_id="call_1", name="write_text_file", status=status)


class VerificationOutcomeTest(unittest.TestCase):
    def setUp(self):
        # 临时目录放在项目根下，确保落在默认工作区域内
        self.tmp = Path(tempfile.mkdtemp(dir=get_project_root(), prefix="_agent_verify_"))
        # 批次日志也指到临时目录，避免测试污染项目根 .agent_state
        self.env = mock.patch.dict(
            os.environ, {"AGENT_STATE_DIR": str(self.tmp / "state")}
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_real_write_is_verified(self):
        target = self.tmp / "note.txt"
        result = write_text_file.invoke({"path": str(target), "content": "hello", "mode": "write"})

        outcome = verify_tool_outcome(
            "write_text_file", {"path": str(target), "content": "hello", "mode": "write"}, result
        )

        self.assertTrue(outcome.checked)
        self.assertTrue(outcome.ok, outcome.note)

    def test_claimed_write_without_file_is_flagged(self):
        target = self.tmp / "missing.txt"
        outcome = verify_tool_outcome(
            "write_text_file", {"path": str(target), "content": "hello"}, "已写入 5 个字符"
        )

        self.assertTrue(outcome.checked)
        self.assertFalse(outcome.ok)
        self.assertIn("不存在", outcome.note)

    def test_append_is_verified_against_tail(self):
        target = self.tmp / "log.txt"
        write_text_file.invoke({"path": str(target), "content": "line1\n", "mode": "write"})
        result = write_text_file.invoke({"path": str(target), "content": "line2\n", "mode": "append"})

        outcome = verify_tool_outcome(
            "write_text_file",
            {"path": str(target), "content": "line2\n", "mode": "append"},
            result,
        )

        self.assertTrue(outcome.ok, outcome.note)

    def test_real_move_is_verified(self):
        src = self.tmp / "a.txt"
        dst = self.tmp / "b.txt"
        src.write_text("x", encoding="utf-8")
        result = move_path.invoke({"source": str(src), "destination": str(dst)})

        outcome = verify_tool_outcome(
            "move_path", {"source": str(src), "destination": str(dst)}, result
        )

        self.assertTrue(outcome.ok, outcome.note)

    def test_real_delete_is_verified(self):
        target = self.tmp / "gone.txt"
        target.write_text("x", encoding="utf-8")
        result = delete_path.invoke({"path": str(target), "permanent": True})

        outcome = verify_tool_outcome("delete_path", {"path": str(target)}, result)

        self.assertTrue(outcome.ok, outcome.note)

    def test_failed_tool_result_is_not_reverified(self):
        # 工具自己已经报错时，不重复做后置校验
        outcome = verify_tool_outcome(
            "write_text_file", {"path": str(self.tmp / "x.txt"), "content": "x"}, "写入失败：权限不足"
        )
        self.assertFalse(outcome.checked)

    def test_unknown_tool_is_skipped(self):
        outcome = verify_tool_outcome("get_weather", {"city": "深圳"}, "晴天")
        self.assertFalse(outcome.checked)

    def test_enrich_marks_failed_verification_as_error(self):
        message = _fake_tool_message("已写入 5 个字符到不存在.txt")
        enriched = enrich_tool_message(
            "write_text_file", {"path": str(self.tmp / "nope.txt"), "content": "hello"}, message
        )

        self.assertIsInstance(enriched, ToolMessage)
        self.assertEqual(enriched.status, "error")
        self.assertIn("结果校验", enriched.content)

    def test_enrich_keeps_untouched_when_not_applicable(self):
        message = _fake_tool_message("晴天")
        enriched = enrich_tool_message("get_weather", {"city": "深圳"}, message)
        self.assertIs(enriched, message)


if __name__ == "__main__":
    unittest.main()
