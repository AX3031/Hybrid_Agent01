"""小企业版 P0 守卫层用例。

覆盖：
- 批次日志与整批回滚（写入/覆盖/移动/复制/删除/建目录/永久删除）；
- 只读总开关；
- 任务卡的动作类别与目录范围；
- 预算/急停；
- 单任务锁；
- 第二主体审批口令与签名凭证；
- 审计字段与命令退出码校验。
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.messages import ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from agent.guard import approval, journal, policy
from agent.guard import state as guard_state
from agent.guard.lock import TaskBusyError, task_lock
from agent.guard.task_card import TaskCard
from agent.tools import safety
from agent.tools.os_tools import (
    copy_path,
    create_directory,
    delete_path,
    move_path,
    read_text_file,
    run_command,
    write_text_file,
)
from agent.tools.verification import verify_tool_outcome
from tests.fakes import ScriptedToolModel
from utils import audit_logger


class GuardTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agent_guard_")).resolve()
        self.workspace = self.tmp / "ws"
        self.workspace.mkdir()
        self.output = self.workspace / "out"
        self.output.mkdir()
        self.state_dir = self.tmp / "state"
        self.state_dir.mkdir()
        self.approval_dir = self.tmp / "approval"
        self.approval_dir.mkdir()

        self.env = mock.patch.dict(
            os.environ,
            {
                "AGENT_STATE_DIR": str(self.state_dir),
                "AGENT_APPROVAL_DIR": str(self.approval_dir),
                "AGENT_READ_ONLY": "0",
            },
        )
        self.env.start()

        self.old_roots = safety.WORKSPACE_ROOTS
        safety.WORKSPACE_ROOTS = [self.workspace]
        self.old_audit_dir = audit_logger._AUDIT_DIR
        audit_logger._AUDIT_DIR = self.tmp / "logs"
        guard_state.reset_for_tests()
        guard_state.clear_stop()

    def tearDown(self):
        guard_state.reset_for_tests()
        guard_state.clear_stop()
        safety.WORKSPACE_ROOTS = self.old_roots
        audit_logger._AUDIT_DIR = self.old_audit_dir
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_card(self, **overrides) -> TaskCard:
        data = {
            "name": "测试任务",
            "user": "小李",
            "goal": "整理 out 目录",
            "success_criteria": ["out 目录文件数符合预期"],
            "input_scope": [str(self.workspace)],
            "output_location": str(self.output),
            "allowed_actions": ["读", "写", "移动", "复制", "删除", "命令"],
            "forbidden_actions": ["发送", "提交", "付款", "提权"],
            "max_steps": 30,
            "max_minutes": 10,
            "max_cost_yuan": 2.0,
            "valid_minutes": 120,
        }
        data.update(overrides)
        return TaskCard(**data)


class BatchRollbackTest(GuardTestBase):
    def test_whole_batch_rollback_restores_files(self):
        target = self.workspace / "a.txt"
        target.write_text("old", encoding="utf-8")
        guard_state.start_task("t1", self.make_card(), batch_id="BTEST-1")

        write_text_file.invoke({"path": str(target), "content": "new", "mode": "write"})
        move_path.invoke({"source": str(target), "destination": str(self.output)})
        moved = self.output / "a.txt"
        copy_path.invoke({"source": str(moved), "destination": str(self.output / "copy.txt")})
        delete_path.invoke({"path": str(self.output / "copy.txt")})
        create_directory.invoke({"path": str(self.output / "newdir")})

        summary = journal.batch_summary("BTEST-1")
        self.assertEqual(summary["ops"], 5, summary)
        self.assertFalse(summary["rolled_back"])

        report = journal.rollback_batch("BTEST-1")
        self.assertTrue(report.ok, report.to_text())
        self.assertEqual(target.read_text(encoding="utf-8"), "old")
        self.assertFalse(moved.exists())
        self.assertFalse((self.output / "copy.txt").exists())
        self.assertFalse((self.output / "newdir").exists())
        self.assertTrue(journal.batch_summary("BTEST-1")["rolled_back"])
        guard_state.end_task()

    def test_permanent_delete_is_reported_as_irreversible(self):
        target = self.workspace / "gone.txt"
        target.write_text("x", encoding="utf-8")
        guard_state.start_task("t2", self.make_card(), batch_id="BTEST-2")
        delete_path.invoke({"path": str(target), "permanent": True})

        report = journal.rollback_batch("BTEST-2")
        self.assertFalse(report.ok)
        self.assertEqual(len(report.irreversible), 1)
        self.assertFalse(target.exists())
        guard_state.end_task()

    def test_append_rollback_truncates_to_previous_size(self):
        target = self.workspace / "log.txt"
        target.write_text("line1\n", encoding="utf-8")
        guard_state.start_task("t3", self.make_card(), batch_id="BTEST-3")
        write_text_file.invoke({"path": str(target), "content": "line2\n", "mode": "append"})

        report = journal.rollback_batch("BTEST-3")
        self.assertTrue(report.ok, report.to_text())
        self.assertEqual(target.read_text(encoding="utf-8"), "line1\n")
        guard_state.end_task()


class ReadOnlyModeTest(GuardTestBase):
    def test_write_is_blocked_and_read_still_works(self):
        target = self.workspace / "note.txt"
        target.write_text("hello", encoding="utf-8")

        with mock.patch.dict(os.environ, {"AGENT_READ_ONLY": "1"}):
            blocked = write_text_file.invoke({"path": str(target), "content": "new"})
            self.assertIn("只读模式", blocked)
            self.assertEqual(target.read_text(encoding="utf-8"), "hello")

            content = read_text_file.invoke({"path": str(target)})
            self.assertIn("hello", content)

    def test_command_and_desktop_input_are_blocked(self):
        with mock.patch.dict(os.environ, {"AGENT_READ_ONLY": "1"}):
            self.assertIn("只读模式", policy.check_read_only("run_command", {"command": "dir"}))
            self.assertIn(
                "只读模式",
                policy.check_read_only("computer_use", {"action": "click"}),
            )
            self.assertEqual(
                policy.check_read_only("computer_use", {"action": "screenshot"}), ""
            )


class TaskCardPolicyTest(GuardTestBase):
    def test_action_not_allowed_is_rejected(self):
        card = self.make_card(allowed_actions=["读"])
        reason = policy.check_task_card(card, "write_text_file", {"path": str(self.output / "x.txt")})
        self.assertIn("没有允许", reason)

    def test_write_outside_output_location_is_rejected(self):
        card = self.make_card()
        reason = policy.check_task_card(
            card, "write_text_file", {"path": str(self.workspace / "elsewhere.txt")}
        )
        self.assertIn("只能写入", reason)

    def test_write_inside_output_location_passes(self):
        card = self.make_card()
        self.assertEqual(
            policy.check_task_card(
                card, "write_text_file", {"path": str(self.output / "ok.txt")}
            ),
            "",
        )

    def test_validation_requires_success_criteria_and_allowed_action(self):
        card = self.make_card(success_criteria=[], allowed_actions=[])
        errors = card.validate()
        self.assertTrue(any("成功判据" in item for item in errors))
        self.assertTrue(any("至少允许一个动作" in item for item in errors))

    def test_output_outside_workspace_is_rejected(self):
        card = self.make_card(output_location=str(self.tmp / "outside"))
        errors = card.validate()
        self.assertTrue(any("输出位置不在工作区域内" in item for item in errors))

    def test_side_effect_without_task_card_is_rejected(self):
        guard_state.reset_for_tests()
        reason = policy.preflight_reason(
            "write_text_file",
            {"path": str(self.output / "x.txt"), "content": "x"},
        )
        self.assertIn("必须先启用任务卡", reason)
        self.assertEqual(
            policy.preflight_reason("list_directory", {"path": str(self.workspace)}),
            "",
        )
        self.assertTrue(policy.requires_task_card("computer_use", {"action": "click"}))
        self.assertFalse(
            policy.requires_task_card("computer_use", {"action": "screenshot"})
        )


class BudgetAndStopTest(GuardTestBase):
    def test_step_limit_blocks_next_tool(self):
        guard_state.start_task("t4", self.make_card(max_steps=1), batch_id="BTEST-4")
        guard_state.consume_step()
        self.assertIn("步数上限", guard_state.budget_reason())
        self.assertIn(
            "步数上限",
            policy.preflight_reason("list_directory", {"path": str(self.workspace)}),
        )
        guard_state.end_task()

    def test_emergency_stop_blocks_and_can_be_cleared(self):
        guard_state.request_stop("测试急停")
        self.assertIn("测试急停", guard_state.stop_reason())
        self.assertIn("任务已停止", policy.preflight_reason("list_directory", {}))
        guard_state.clear_stop()
        self.assertEqual(guard_state.stop_reason(), "")

    def test_task_stopped_cannot_start_until_cleared(self):
        guard_state.request_stop("先停一下")
        with self.assertRaises(guard_state.TaskStopped):
            guard_state.start_task("t5", self.make_card())
        guard_state.clear_stop()


class SingleTaskLockTest(GuardTestBase):
    def test_second_task_is_rejected_then_allowed_after_release(self):
        with task_lock("first"):
            with self.assertRaises(TaskBusyError):
                with task_lock("second"):
                    pass
        with task_lock("third"):
            pass


class SecondSubjectTest(GuardTestBase):
    def test_pin_hash_never_contains_plaintext(self):
        approval.set_pin("Abc12345")
        self.assertTrue(approval.pin_configured())
        self.assertTrue(approval.verify_pin("Abc12345"))
        self.assertFalse(approval.verify_pin("wrong"))
        text = (self.approval_dir / "pin.json").read_text(encoding="utf-8")
        self.assertNotIn("Abc12345", text)

    def test_approval_token_is_signed_and_verifiable(self):
        approval.set_pin("Abc12345")
        actions = [{"name": "run_command", "args": {"command": "dir"}}]
        token = approval.sign_approval("t1", "B1", actions)
        self.assertTrue(approval.verify_approval_token(token))
        self.assertFalse(approval.verify_approval_token("not-a-token"))

    def test_approval_dir_is_blocked_for_agent_tools(self):
        approval.set_pin("Abc12345")
        pin_path = self.approval_dir / "pin.json"
        result = read_text_file.invoke({"path": str(pin_path)})
        self.assertIn("审批凭据目录", result)

    def test_weak_pin_is_rejected(self):
        with self.assertRaises(ValueError):
            approval.set_pin("123456")


class AuditAndVerificationTest(GuardTestBase):
    def test_audit_record_contains_batch_and_approver_fields(self):
        audit_logger.log_tool_call(
            "s1",
            "write_text_file",
            {"path": "x"},
            "ok",
            batch_id="B1",
            task_id="T1",
            step_no=3,
            reversibility="R1",
            approver="human",
            approval_token="abc",
        )
        lines = list((self.tmp / "logs").glob("audit_*.jsonl"))
        self.assertEqual(len(lines), 1)
        records = [
            json.loads(line)
            for line in lines[0].read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        record = next(item for item in records if item.get("event") == "tool_call")
        self.assertEqual(record["batch_id"], "B1")
        self.assertEqual(record["step_no"], 3)
        self.assertEqual(record["reversibility"], "R1")
        self.assertEqual(record["approver"], "human")
        self.assertEqual(record["policy_version"], "small-business-p0-v1")

    def test_command_exit_code_is_checked(self):
        failed = verify_tool_outcome(
            "run_command", {}, "命令：echo\n退出码：1\n标准错误：\nboom"
        )
        self.assertTrue(failed.checked)
        self.assertFalse(failed.ok)

        passed = verify_tool_outcome("run_command", {}, "命令：echo\n退出码：0")
        self.assertTrue(passed.checked)
        self.assertTrue(passed.ok)

    def test_desktop_action_is_marked_unconfirmed(self):
        outcome = verify_tool_outcome("computer_use", {"action": "click"}, "已点击 (1, 2)")
        self.assertTrue(outcome.checked)
        self.assertTrue(outcome.ok)
        self.assertFalse(outcome.confirmed)

    def test_copy_content_mismatch_is_flagged(self):
        src = self.workspace / "copy-src.txt"
        dst = self.workspace / "copy-dst.txt"
        src.write_text("good", encoding="utf-8")
        dst.write_text("bad", encoding="utf-8")
        outcome = verify_tool_outcome(
            "copy_path",
            {"source": str(src), "destination": str(dst)},
            "已复制：src -> dst",
        )
        self.assertTrue(outcome.checked)
        self.assertFalse(outcome.ok)
        self.assertIn("不一致", outcome.note)


class GuardMiddlewareOrderTest(GuardTestBase):
    """确认“批准后才算一步”：预检在人工确认前，扣步在确认后。"""

    def _build_graph(self, target: Path):
        from agent.guard.middlewares import guard_execute, guard_preflight
        from agent.tools.middleware import monitor_tool

        model = ScriptedToolModel(
            tool_name="write_text_file",
            tool_args={"path": str(target), "content": "approved"},
            final_text="完成",
        )
        graph = create_agent(
            model,
            tools=[write_text_file],
            middleware=[
                monitor_tool,
                guard_preflight,
                HumanInTheLoopMiddleware(
                    interrupt_on={
                        "write_text_file": {"allowed_decisions": ["approve", "reject"]}
                    }
                ),
                guard_execute,
            ],
            checkpointer=InMemorySaver(),
        )
        return graph, {"configurable": {"thread_id": "guard-order"}}

    def test_step_is_consumed_only_after_approval(self):
        target = self.output / "approved.txt"
        guard_state.start_task("g1", self.make_card(), batch_id="BGUARD-1")
        graph, config = self._build_graph(target)

        first = list(graph.stream({"messages": [{"role": "user", "content": "写"}]}, config=config))
        self.assertTrue(any(isinstance(chunk, dict) and "__interrupt__" in chunk for chunk in first))
        self.assertEqual(guard_state.current().steps, 0, "等待确认不应扣步数")
        self.assertFalse(target.exists())

        list(
            graph.stream(
                Command(resume={"decisions": [{"type": "approve"}]}),
                config=config,
            )
        )
        self.assertEqual(guard_state.current().steps, 1, "批准并执行后才扣步数")
        self.assertTrue(target.exists())
        guard_state.end_task()

    def test_rejected_action_does_not_consume_step(self):
        target = self.output / "rejected.txt"
        guard_state.start_task("g2", self.make_card(), batch_id="BGUARD-2")
        graph, config = self._build_graph(target)

        list(graph.stream({"messages": [{"role": "user", "content": "写"}]}, config=config))
        list(
            graph.stream(
                Command(resume={"decisions": [{"type": "reject", "message": "不执行"}]}),
                config=config,
            )
        )
        self.assertEqual(guard_state.current().steps, 0)
        self.assertFalse(target.exists())
        guard_state.end_task()

    def test_budget_stop_short_circuits_without_calling_model(self):
        from agent.guard.middlewares import guard_model

        guard_state.start_task("g3", self.make_card(max_steps=1), batch_id="BGUARD-3")
        guard_state.consume_step()
        model = ScriptedToolModel(final_text="模型不应该被调用")
        graph = create_agent(
            model,
            tools=[write_text_file],
            middleware=[guard_model],
            checkpointer=InMemorySaver(),
        )
        result = graph.invoke(
            {"messages": [{"role": "user", "content": "继续"}]},
            config={"configurable": {"thread_id": "guard-budget"}},
        )
        self.assertIn("任务已停止", result["messages"][-1].content)
        self.assertEqual(model.call_seq, 0, "预算超限后不应再调用模型")
        guard_state.end_task()

    def test_r2_action_without_pin_token_is_rejected(self):
        from agent.guard.middlewares import guard_execute, guard_preflight
        from agent.tools.middleware import monitor_tool

        model = ScriptedToolModel(
            tool_name="run_command",
            tool_args={"command": "echo hi"},
            final_text="完成",
        )
        graph = create_agent(
            model,
            tools=[run_command],
            middleware=[
                monitor_tool,
                guard_preflight,
                HumanInTheLoopMiddleware(
                    interrupt_on={
                        "run_command": {"allowed_decisions": ["approve", "reject"]}
                    }
                ),
                guard_execute,
            ],
            checkpointer=InMemorySaver(),
        )
        config = {"configurable": {"thread_id": "guard-pin"}}
        guard_state.start_task("g4", self.make_card(), batch_id="BGUARD-4")

        list(graph.stream({"messages": [{"role": "user", "content": "执行"}]}, config=config))
        list(
            graph.stream(
                Command(resume={"decisions": [{"type": "approve"}]}),
                config=config,
            )
        )

        messages = [
            message
            for message in graph.get_state(config).values["messages"]
            if isinstance(message, ToolMessage)
        ]
        self.assertTrue(messages)
        self.assertEqual(messages[-1].status, "error")
        self.assertIn("审批凭证", messages[-1].content)
        guard_state.end_task()


if __name__ == "__main__":
    unittest.main()
