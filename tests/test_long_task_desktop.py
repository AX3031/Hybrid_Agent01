"""让「一连串桌面要求」跑得起来的三项支撑：截图历史裁剪、原地打转检测、桌面预算。

对应真实事故：任务「打开微信，登录后给好友邓三三发个你好」在屏幕左下角
连点 5 次、每次重新截图，12 步就把 2 元预算烧光被熔断。
"""

import tempfile
import unittest
from pathlib import Path
from shutil import rmtree
from types import SimpleNamespace
from unittest import mock

from langchain_core.messages import HumanMessage, ToolMessage

from agent.guard import config as guard_config
from agent.guard import policy
from agent.guard import repeat_guard
from agent.guard import state as guard_state
from agent.guard.middlewares import guard_execute
from agent.guard.task_card import TaskCard, split_subgoals
from agent.tools.middleware import trim_messages_screenshots, trim_screenshot_history


def _screenshot_message(step: int) -> ToolMessage:
    return ToolMessage(
        content=[
            {"type": "text", "text": f"Screenshot captured，截图文件：x/{step}.png"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{step}"}},
        ],
        tool_call_id=f"call-{step}",
        name="computer_use",
    )


class TrimScreenshotHistoryTest(unittest.TestCase):
    def test_only_recent_images_are_kept(self):
        messages = [HumanMessage(content="打开微信")]
        messages += [_screenshot_message(index) for index in range(1, 5)]
        trimmed, dropped = trim_messages_screenshots(messages, keep=1)
        self.assertEqual(dropped, 3)
        self.assertIs(trimmed[0], messages[0])
        self.assertIn("base64,4", str(trimmed[-1].content))
        for message in trimmed[1:-1]:
            self.assertNotIn("image_url", str(message.content))
            self.assertIn("历史截图已省略", str(message.content))
        # 文字部分（截图路径/结果摘要）必须保留，模型仍知道那一步做了什么
        self.assertIn("x/1.png", str(trimmed[1].content))

    def test_original_messages_are_not_mutated(self):
        messages = [_screenshot_message(index) for index in range(1, 4)]
        trim_messages_screenshots(messages, keep=1)
        self.assertEqual(len(messages), 3)
        for message in messages:
            self.assertIn("image_url", str(message.content))

    def test_fewer_images_than_keep_is_noop(self):
        messages = [_screenshot_message(1), _screenshot_message(2)]
        trimmed, dropped = trim_messages_screenshots(messages, keep=3)
        self.assertEqual(dropped, 0)
        self.assertIs(trimmed, messages)

    def test_negative_keep_disables_trimming(self):
        messages = [_screenshot_message(index) for index in range(1, 6)]
        trimmed, dropped = trim_messages_screenshots(messages, keep=-1)
        self.assertEqual((trimmed, dropped), (messages, 0))


class KeepRecentScreenshotsConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vision_cfg_")).resolve()
        self.config_path = self.tmp / "agent.yml"
        self.patcher = mock.patch.object(guard_config, "_CONFIG_PATH", self.config_path)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.addCleanup(lambda: rmtree(self.tmp, ignore_errors=True))

    def _write(self, body: str):
        self.config_path.write_text(body, encoding="utf-8")

    def test_default_is_one(self):
        self._write("read_only: false\n")
        self.assertEqual(guard_config.keep_recent_screenshots(), 1)

    def test_configured_value(self):
        self._write("vision_history:\n  keep_recent_screenshots: 3\n")
        self.assertEqual(guard_config.keep_recent_screenshots(), 3)

    def test_zero_is_treated_as_one(self):
        self._write("vision_history:\n  keep_recent_screenshots: 0\n")
        self.assertEqual(guard_config.keep_recent_screenshots(), 1)

    def test_negative_disables(self):
        self._write("vision_history:\n  keep_recent_screenshots: -1\n")
        self.assertEqual(guard_config.keep_recent_screenshots(), -1)

    def test_garbage_value_falls_back(self):
        self._write("vision_history:\n  keep_recent_screenshots: 很多\n")
        self.assertEqual(guard_config.keep_recent_screenshots(), 1)


class RepeatGuardTest(unittest.TestCase):
    def setUp(self):
        repeat_guard.reset_for_tests()
        self.addCleanup(repeat_guard.reset_for_tests)

    def _click(self, batch: str, x: int, y: int) -> tuple[int, str, str]:
        return repeat_guard.note_action(batch, "computer_use", {"action": "click", "x": x, "y": y})

    def test_observation_actions_are_not_tracked(self):
        count, advice, blocked = repeat_guard.note_action("B1", "computer_use", {"action": "screenshot"})
        self.assertEqual((count, advice, blocked), (0, "", ""))

    def test_distant_clicks_do_not_count_as_loop(self):
        self._click("B1", 200, 200)
        _count, advice, _blocked = self._click("B1", 900, 700)
        self.assertEqual(advice, "")

    def test_nearby_repeats_warn_then_block(self):
        # 复刻审计日志里的点位：同一小片区域反复点击
        points = [(200, 975), (190, 980), (195, 970), (185, 980), (190, 975), (178, 985)]
        advisories = [self._click("B1", x, y) for x, y in points]
        self.assertEqual(advisories[0][1], "")
        self.assertIn("重复动作提醒", advisories[2][1])
        self.assertIn("app_open", advisories[2][1])
        self.assertIn("原地打转", advisories[5][2])

    def test_same_key_presses_count_as_loop(self):
        for index in range(3):
            _count, advice, _blocked = repeat_guard.note_action(
                "B2", "computer_use", {"action": "hotkey", "keys": ["alt", "tab"]}
            )
        self.assertIn("重复动作提醒", advice)

    def test_different_text_inputs_are_not_a_loop(self):
        first = repeat_guard.note_action("B3", "computer_use", {"action": "type_text", "text": "你好"})
        second = repeat_guard.note_action("B3", "computer_use", {"action": "type_text", "text": "在吗"})
        self.assertEqual(first[1], "")
        self.assertEqual(second[1], "")

    def test_new_batch_resets_history(self):
        for _ in range(5):
            self._click("B4", 100, 100)
        count, advice, blocked = self._click("B5", 100, 100)
        self.assertEqual((count, advice, blocked), (1, "", ""))

    def test_non_desktop_tools_are_ignored(self):
        for _ in range(8):
            count, advice, blocked = repeat_guard.note_action(
                "B6", "read_text_file", {"path": "a.txt"}
            )
        self.assertEqual((count, advice, blocked), (0, "", ""))


class DesktopTaskBudgetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="desk_cfg_")).resolve()
        self.config_path = self.tmp / "agent.yml"
        self.config_path.write_text("read_only: false\n", encoding="utf-8")
        self.patcher = mock.patch.object(guard_config, "_CONFIG_PATH", self.config_path)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.addCleanup(lambda: rmtree(self.tmp, ignore_errors=True))

    def test_desktop_prompt_gets_wider_budget(self):
        card = TaskCard.from_prompt("打开微信，登录后给好友邓三三发个你好")
        self.assertEqual(card.max_steps, 60)
        self.assertEqual(card.max_minutes, 20)
        self.assertEqual(card.max_cost_yuan, 5.0)
        self.assertEqual(card.validate(), [])

    def test_read_only_prompt_keeps_generic_budget(self):
        card = TaskCard.from_prompt("看看这台电脑的系统信息", allowed_actions=["读"])
        self.assertEqual(card.max_steps, 30)
        self.assertEqual(card.max_minutes, 10)
        self.assertEqual(card.max_cost_yuan, 2.0)

    def test_configured_desktop_budget_is_respected_but_not_below_generic(self):
        self.config_path.write_text(
            "task_defaults:\n"
            "  max_steps: 40\n"
            "  max_minutes: 15\n"
            "  max_cost_yuan: 3.0\n"
            "  desktop_max_steps: 10\n"
            "  desktop_max_minutes: 5\n"
            "  desktop_max_cost_yuan: 1.0\n",
            encoding="utf-8",
        )
        card = TaskCard.from_prompt("打开微信")
        # 桌面默认值不能低于通用值，否则等于把预算调没了
        self.assertEqual(card.max_steps, 40)
        self.assertEqual(card.max_minutes, 15)
        self.assertEqual(card.max_cost_yuan, 3.0)


class GuardExecuteWiringTest(unittest.TestCase):
    """确认 guard_execute 真的把打转检测接进了执行链路（提醒 + 拦截）。"""

    def setUp(self):
        repeat_guard.reset_for_tests()
        self.tmp = Path(tempfile.mkdtemp(prefix="repeat_state_")).resolve()
        self.env = mock.patch.dict("os.environ", {"AGENT_STATE_DIR": str(self.tmp)})
        self.env.start()
        self.pin = mock.patch.object(policy, "requires_second_subject", lambda *args, **kwargs: False)
        self.pin.start()
        guard_state.reset_for_tests()
        guard_state.start_task(
            "repeat-thread",
            TaskCard.from_prompt("打开微信，登录后给好友邓三三发个你好"),
            batch_id="BREPEAT-1",
        )
        self.addCleanup(guard_state.end_task)
        self.addCleanup(guard_state.reset_for_tests)
        self.addCleanup(repeat_guard.reset_for_tests)
        self.addCleanup(lambda: rmtree(self.tmp, ignore_errors=True))
        self.addCleanup(self.env.stop)
        self.addCleanup(self.pin.stop)

    def _run_click(self, x: int, y: int) -> ToolMessage:
        request = SimpleNamespace(
            tool_call={
                "name": "computer_use",
                "args": {"action": "click", "x": x, "y": y},
                "id": f"call-{x}-{y}",
            }
        )

        def handler(_request):
            return ToolMessage(content="Clicked", tool_call_id="call", name="computer_use")

        return guard_execute.wrap_tool_call(request, handler)

    def test_third_repeat_gets_advice_in_tool_result(self):
        self.assertEqual(self._run_click(200, 975).content, "Clicked")
        self.assertEqual(self._run_click(190, 980).content, "Clicked")
        message = self._run_click(195, 970)
        self.assertIn("Clicked", message.content)
        self.assertIn("重复动作提醒", message.content)
        self.assertIn("app_open_start_menu", message.content)

    def test_sixth_repeat_is_not_executed(self):
        for x, y in [(200, 975), (190, 980), (195, 970), (185, 980), (190, 975)]:
            self._run_click(x, y)
        steps_before = guard_state.current().steps
        message = self._run_click(178, 985)
        self.assertEqual(message.status, "error")
        self.assertIn("原地打转", message.content)
        # 被拦截的那次不记步数，不给预算增加开销
        self.assertEqual(guard_state.current().steps, steps_before)


class _FakeModelRequest:
    """只实装中间件用到的两个属性，避开真实 ModelRequest 的构造依赖。"""

    def __init__(self, messages):
        self.messages = list(messages)

    def override(self, **kwargs):
        return _FakeModelRequest(kwargs["messages"])


class TrimMiddlewareWiringTest(unittest.TestCase):
    def test_handler_receives_trimmed_messages(self):
        messages = [_screenshot_message(index) for index in range(1, 4)]
        seen = {}

        def handler(request):
            seen["messages"] = request.messages
            return "ok"

        result = trim_screenshot_history.wrap_model_call(_FakeModelRequest(messages), handler)
        self.assertEqual(result, "ok")
        self.assertIn("base64,3", str(seen["messages"][-1].content))
        self.assertNotIn("image_url", str(seen["messages"][0].content))
        # 原历史（checkpoint 里的）不受影响，审计仍能看到每一张
        self.assertIn("image_url", str(messages[0].content))

    def test_no_images_passes_request_through(self):
        messages = [HumanMessage(content="你好")]
        passed = {}

        def handler(request):
            passed["request"] = request
            return "ok"

        request = _FakeModelRequest(messages)
        trim_screenshot_history.wrap_model_call(request, handler)
        self.assertIs(passed["request"], request)


class SplitSubgoalsTest(unittest.TestCase):
    def test_chained_request_is_split_in_order(self):
        self.assertEqual(
            split_subgoals("打开微信，登录后给好友邓三三发个你好"),
            ["打开微信", "登录后给好友邓三三发个你好"],
        )

    def test_connectors_are_split(self):
        items = split_subgoals("打开记事本然后写一句话接着保存")
        self.assertEqual(items, ["打开记事本", "写一句话", "保存"])

    def test_single_goal_returns_empty(self):
        self.assertEqual(split_subgoals("打开微信"), [])
        self.assertEqual(split_subgoals(""), [])

    def test_short_fragments_are_dropped(self):
        self.assertEqual(split_subgoals("打开微信，好"), [])

    def test_quick_card_carries_subgoals_into_prompt(self):
        card = TaskCard.from_prompt("打开微信，登录后给好友邓三三发个你好")
        self.assertEqual(len(card.subgoals), 2)
        prompt = card.to_prompt()
        self.assertIn("一连串要求", prompt)
        self.assertIn("  1. 打开微信", prompt)
        self.assertIn("  2. 登录后给好友邓三三发个你好", prompt)
        self.assertIn("登录", prompt)

    def test_manual_card_without_subgoals_still_gets_guidance(self):
        card = TaskCard(
            name="复合任务",
            user="",
            goal="打开浏览器，搜索天气，把结果写成文件",
            success_criteria=["完成"],
            allowed_actions=["读", "写", "桌面"],
        )
        self.assertEqual(card.subgoals, [])
        self.assertIn("  3. 把结果写成文件", card.to_prompt())

    def test_serialisation_keeps_subgoals(self):
        card = TaskCard.from_prompt("打开微信，登录后给好友邓三三发个你好")
        restored = TaskCard.from_dict(card.to_dict())
        self.assertEqual(restored.subgoals, card.subgoals)


if __name__ == "__main__":
    unittest.main()
