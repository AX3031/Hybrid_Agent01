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
import hashlib
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
from agent.guard import config as guard_config
from agent.guard import state as guard_state
from agent.guard import criteria, scope
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
from utils.path_tool import get_project_root


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
            "machine_checks": [f"exists|{self.output}"],
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
        self.assertIn("当前任务没有任务卡", reason)
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

    def test_approval_token_is_bound_to_thread_batch_and_actions(self):
        approval.set_pin("Abc12345")
        actions = [{"name": "run_command", "args": {"command": "dir"}}]
        token = approval.sign_approval("t1", "B1", actions)
        self.assertTrue(
            approval.verify_approval_token(
                token, thread_id="t1", batch_id="B1", actions=actions
            )
        )
        self.assertFalse(
            approval.verify_approval_token(
                token, thread_id="t2", batch_id="B1", actions=actions
            )
        )
        self.assertFalse(
            approval.verify_approval_token(
                token, thread_id="t1", batch_id="B2", actions=actions
            )
        )
        self.assertFalse(
            approval.verify_approval_token(
                token,
                thread_id="t1",
                batch_id="B1",
                actions=[{"name": "run_command", "args": {"command": "whoami"}}],
            )
        )
        self.assertFalse(approval.verify_approval_token(token, max_age_seconds=-10))

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


class ProjectProtectionTest(GuardTestBase):
    def test_project_protected_paths_are_recognized(self):
        root = Path(get_project_root()).resolve()
        protected = [
            "agent/guard/policy.py",
            "config/agent.yml",
            "prompts/main_prompt.txt",
            "scripts/apply_env_patch.py",
            "utils/audit_logger.py",
            "model/factory.py",
            "env_patch/sitecustomize.py",
            "tests/test_small_business_guard.py",
            ".venv/Lib/site-packages/sitecustomize.py",
            ".git/config",
            "logs/audit_20261027.jsonl",
            "memorys/chat_history/agent.json",
            "data/task_skills/skills.json",
            ".env",
        ]
        for relative in protected:
            self.assertTrue(
                safety.is_project_protected(root / relative),
                relative,
            )
        self.assertFalse(safety.is_project_protected(root / "data" / "output.txt"))

    def test_project_protected_path_cannot_be_written(self):
        root = Path(get_project_root()).resolve()
        old_roots = safety.WORKSPACE_ROOTS
        try:
            safety.WORKSPACE_ROOTS = [root]
            path, error = safety.guard_path("config/agent.yml", for_write=True)
            self.assertIsNone(path)
            self.assertIn("项目源码", error)
            path, error = safety.guard_path("data/output.txt", for_write=True)
            self.assertIsNotNone(path)
            self.assertEqual(error, "")
            path, error = safety.guard_path("data/task_skills/skills.json", for_write=True)
            self.assertIsNone(path)
            self.assertIn("项目源码", error)
        finally:
            safety.WORKSPACE_ROOTS = old_roots

    def test_command_cannot_touch_project_protected_paths(self):
        self.assertIn("项目源码", safety.check_command("Set-Content config/agent.yml x"))
        self.assertIn("项目源码", safety.check_command("copy a.txt logs/x.txt"))
        self.assertIn("项目源码", safety.check_command("copy a.txt data/task_skills/skills.json"))
        self.assertIn("复制/移动为可执行文件", safety.check_command("copy a.txt b.bat"))
        self.assertEqual(safety.check_command("dir data"), "")


class CopyMoveSuffixTest(GuardTestBase):
    def test_copy_to_executable_suffix_is_rejected(self):
        source = self.workspace / "note.txt"
        source.write_text("hello", encoding="utf-8")
        result = copy_path.invoke(
            {"source": str(source), "destination": str(self.workspace / "note.bat")}
        )
        self.assertIn("可执行文件类型", result)
        self.assertFalse((self.workspace / "note.bat").exists())

    def test_move_to_executable_suffix_is_rejected(self):
        source = self.workspace / "move.txt"
        source.write_text("hello", encoding="utf-8")
        result = move_path.invoke(
            {"source": str(source), "destination": str(self.workspace / "move.ps1")}
        )
        self.assertIn("可执行文件类型", result)
        self.assertTrue(source.exists())
        self.assertFalse((self.workspace / "move.ps1").exists())

    def test_directory_copy_with_executable_inside_is_rejected(self):
        source_dir = self.workspace / "source_dir"
        source_dir.mkdir()
        (source_dir / "payload.bat").write_text("@echo off", encoding="utf-8")
        destination = self.output / "copied_dir"
        result = copy_path.invoke(
            {"source": str(source_dir), "destination": str(destination)}
        )
        self.assertIn("可执行文件类型", result)
        self.assertFalse(destination.exists())


class GuiSemanticForbiddenTest(GuardTestBase):
    def test_gui_mutation_is_blocked_when_card_forbids_send_or_pay(self):
        card = self.make_card(allowed_actions=["桌面"], forbidden_actions=["发送", "付款"])
        reason = policy.check_task_card(
            card,
            "computer_use",
            {"action": "type_text", "text": "hi"},
        )
        self.assertIn("无法判断", reason)

    def test_gui_mutation_passes_when_semantic_forbidden_is_removed(self):
        card = self.make_card(allowed_actions=["桌面"], forbidden_actions=[])
        self.assertEqual(
            policy.check_task_card(
                card,
                "computer_use",
                {"action": "type_text", "text": "hi"},
            ),
            "",
        )
        self.assertEqual(
            policy.check_task_card(
                card,
                "computer_use",
                {"action": "screenshot"},
            ),
            "",
        )


class TaskCardMachineCheckTest(GuardTestBase):
    def test_write_card_can_omit_machine_check(self):
        card = self.make_card(machine_checks=[], allowed_actions=["写"])
        self.assertEqual(card.validate(), [])

    def test_readonly_card_can_omit_machine_check(self):
        card = self.make_card(machine_checks=[], allowed_actions=["读"])
        self.assertEqual(card.validate(), [])

    def test_quick_card_needs_no_form_and_keeps_gui_actions(self):
        card = TaskCard.from_prompt("打开记事本并写一段话")
        self.assertEqual(card.validate(), [])
        self.assertIn("桌面", card.allowed_actions)
        self.assertEqual(card.machine_checks, [])
        self.assertIn("打开记事本", card.goal)
        self.assertNotIn("发送", card.forbidden_actions)
        self.assertIn("提权", card.forbidden_actions)

    def test_quick_card_can_carry_skill_steps(self):
        card = TaskCard.from_prompt(
            "导出报表",
            reference_steps=["1. 打开应用", "2. 点击导出"],
        )
        prompt = card.to_prompt()
        self.assertIn("可复用技能参考", prompt)
        self.assertIn("点击导出", prompt)

    def test_machine_checks_pass_with_deterministic_evidence(self):
        target = self.output / "result.txt"
        target.write_text("hello", encoding="utf-8")
        checks = [
            f"exists|{target}",
            f"file_count|{self.output}|1",
            f"file_size|{target}|5",
            f"sha256|{target}|{hashlib.sha256(b'hello').hexdigest()}",
            f"text_contains|{target}|hello",
            f"absent|{self.output / 'missing.txt'}",
            f"text_not_contains|{target}|bye",
        ]
        report = criteria.criteria_report(checks)
        self.assertEqual(report["status"], "passed", report["text"])

    def test_machine_check_failure_is_not_completion(self):
        target = self.output / "result.txt"
        target.write_text("hello", encoding="utf-8")
        report = criteria.criteria_report([f"text_contains|{target}|bye"])
        self.assertEqual(report["status"], "failed")

    def test_machine_check_invalid_path_is_rejected_by_card(self):
        card = self.make_card(machine_checks=[f"exists|{self.tmp / 'outside'}"])
        errors = card.validate()
        self.assertTrue(any("机器判据路径不在工作区域内" in item for item in errors))

    def test_rollback_boundary_is_explicit(self):
        self.assertTrue(scope.is_rollbackable("write_text_file"))
        self.assertFalse(scope.is_rollbackable("run_command"))
        self.assertFalse(
            scope.is_rollbackable("delete_path", {"permanent": True})
        )
        self.assertIn("不可回滚", scope.ROLLBACK_BOUNDARY_TEXT)

    def test_task_card_prompt_contains_machine_checks_and_boundary(self):
        card = self.make_card(machine_checks=[f"exists|{self.output}"])
        prompt = card.to_prompt()
        self.assertIn("机器判据", prompt)
        self.assertIn("回滚边界", prompt)
        self.assertIn("语义禁令", prompt)

    def test_verification_failure_marks_task_not_finished(self):
        guard_state.start_task("verify-failed", self.make_card(), batch_id="BVERIFY-1")
        guard_state.mark_verification("failed")
        self.assertEqual(guard_state.current().status, "verification_failed")
        guard_state.end_task("finished")
        self.assertIsNone(guard_state.current())

    def test_quick_task_without_machine_checks_can_finish(self):
        guard_state.start_task(
            "verify-quick",
            TaskCard.from_prompt("打开记事本"),
            batch_id="BVERIFY-2",
        )
        guard_state.mark_verification("not_required")
        self.assertEqual(guard_state.current().status, "running")
        guard_state.end_task("finished")
        self.assertIsNone(guard_state.current())


class TaskCardRequiredTest(GuardTestBase):
    def test_new_task_without_card_is_rejected(self):
        with self.assertRaises(ValueError):
            guard_state.start_task("no-card", None)

    def test_quick_card_can_start_a_task(self):
        task = guard_state.start_task(
            "quick-card",
            TaskCard.from_prompt("查看桌面"),
            batch_id="BQUICK-1",
        )
        self.assertEqual(task.card["goal"], "查看桌面")
        guard_state.end_task()


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

    def _build_r2_graph(self):
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
        return graph, {"configurable": {"thread_id": "guard-pin"}}

    def test_dummy_token_is_rejected_not_just_checked_for_presence(self):
        guard_state.start_task("g5", self.make_card(), batch_id="BGUARD-5")
        graph, config = self._build_r2_graph()
        list(graph.stream({"messages": [{"role": "user", "content": "执行"}]}, config=config))
        guard_state.set_approval_context(
            "dummy-token",
            "human",
            [{"name": "run_command", "args": {"command": "echo hi"}}],
            thread_id="g5",
            batch_id="BGUARD-5",
        )
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

    def test_valid_bound_token_is_accepted(self):
        guard_state.start_task("g6", self.make_card(), batch_id="BGUARD-6")
        graph, config = self._build_r2_graph()
        list(graph.stream({"messages": [{"role": "user", "content": "执行"}]}, config=config))
        actions = [{"name": "run_command", "args": {"command": "echo hi"}}]
        token = approval.sign_approval("g6", "BGUARD-6", actions)
        guard_state.set_approval_context(
            token,
            "human",
            actions,
            thread_id="g6",
            batch_id="BGUARD-6",
        )
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
        self.assertEqual(messages[-1].status, "success", messages[-1].content)
        guard_state.end_task()


class AgentConfigBlockTest(unittest.TestCase):
    """修改工作区域/只读开关时，不能重排整份 agent.yml。"""

    SAMPLE = (
        "external_data_path: data/external/records.csv\n"
        "\n"
        "# 电脑操作的工作区域注释。\n"
        "workspace_roots:\n"
        '  - "~"\n'
        '  - "."\n'
        "\n"
        "# 只读总开关。\n"
        "read_only: false\n"
        "\n"
        "# 敏感目录注释。\n"
        "sensitive_dirs:\n"
        '  - ".ssh"\n'
        '  - ".aws"\n'
    )

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agent_cfg_")).resolve()
        self.config_path = self.tmp / "agent.yml"
        self.config_path.write_text(self.SAMPLE, encoding="utf-8")
        self.patcher = mock.patch.object(
            guard_config, "_CONFIG_PATH", self.config_path
        )
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_update_block_preserves_comments_and_other_keys(self):
        guard_config.update_agent_config_block(
            "workspace_roots", ["C:\\Users\\me\\Desktop"]
        )
        text = self.config_path.read_text(encoding="utf-8")
        # 注释与无关配置必须原样保留
        self.assertIn("# 电脑操作的工作区域注释", text)
        self.assertIn("# 只读总开关", text)
        self.assertIn("# 敏感目录注释", text)
        self.assertIn('  - ".ssh"', text)
        self.assertIn('  - ".aws"', text)
        self.assertIn("external_data_path: data/external/records.csv", text)
        # 工作区域已更新，旧的 ~ / . 不再残留
        self.assertIn("C:\\Users\\me\\Desktop", text)
        self.assertNotIn('  - "~"', text)
        reloaded = guard_config.read_agent_config()
        self.assertEqual(reloaded["workspace_roots"], ["C:\\Users\\me\\Desktop"])
        self.assertEqual(reloaded["sensitive_dirs"], [".ssh", ".aws"])
        self.assertFalse(reloaded["read_only"])

    def test_set_config_value_only_touches_target_key(self):
        guard_config.set_config_value("read_only", True)
        text = self.config_path.read_text(encoding="utf-8")
        self.assertIn("# 电脑操作的工作区域注释", text)
        self.assertIn('  - "~"', text)  # 工作区域不受影响
        self.assertIn("read_only: true", text)
        self.assertTrue(guard_config.read_agent_config()["read_only"])


class TaskCardCoercionTest(GuardTestBase):
    """任务卡对象 / dict / 鸭子对象都要能进守卫层。

    真实事故：Streamlit 热重载后 `isinstance(card, TaskCard)` 为 False，
    `_card_to_dict` 走到 `dict(card)`，确认后继续执行直接报
    `'TaskCard' object is not iterable`。
    """

    class _DuckCard:
        """长得像任务卡、但不是同一个类（模拟热重载后的旧/新类）。"""

        def __init__(self, data: dict):
            self._data = data

        def to_dict(self) -> dict:
            return dict(self._data)

    def test_start_task_accepts_task_card_object(self):
        card = self.make_card()
        task = guard_state.start_task("coerce-1", card, batch_id="BCOERCE-1")
        self.assertIsInstance(task.card, dict)
        self.assertEqual(task.card["goal"], card.goal)
        self.assertEqual(guard_state.current_card().goal, card.goal)
        guard_state.end_task()

    def test_start_task_accepts_duck_typed_card(self):
        card = self.make_card()
        duck = self._DuckCard(card.to_dict())
        task = guard_state.start_task("coerce-2", duck, batch_id="BCOERCE-2")
        self.assertEqual(task.card["name"], card.name)
        guard_state.end_task()

    def test_resume_accepts_task_card_object(self):
        card = self.make_card()
        guard_state.start_task("coerce-3", card, batch_id="BCOERCE-3")
        guard_state.pause_task()
        # 模拟“确认后继续”：快照已在盘上，再次传对象进来
        task = guard_state.start_task("coerce-3", card, batch_id="BCOERCE-3", resume=True)
        self.assertEqual(task.batch_id, "BCOERCE-3")
        self.assertEqual(task.card["goal"], card.goal)
        guard_state.end_task()

    def test_invalid_type_raises_readable_error(self):
        with self.assertRaises(TypeError) as ctx:
            guard_state.start_task("coerce-4", 123, batch_id="BCOERCE-4")
        message = str(ctx.exception)
        self.assertIn("任务卡类型不支持", message)
        self.assertIn("int", message)

    def test_from_dict_accepts_task_card_object(self):
        card = self.make_card()
        restored = TaskCard.from_dict(card)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.goal, card.goal)

    def test_from_dict_ignores_non_mapping(self):
        self.assertIsNone(TaskCard.from_dict([]))
        self.assertIsNone(TaskCard.from_dict("not a card"))


if __name__ == "__main__":
    unittest.main()
