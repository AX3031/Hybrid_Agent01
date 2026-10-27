"""UI 自动化 / OCR 动作的守卫与参数用例（不真的操作系统界面）。

重点覆盖三件事：
- 终端/系统工具窗口必须在“动手之前”被拒绝（不能先 Invoke 再检查）；
- 密码框不读不写；
- 自绘界面拿不到控件时，参数与提示要能引导模型改用 OCR。
"""

import os
import unittest
from unittest import mock

from agent.guard import policy
from agent.tools import computer_use
from agent.tools.ui_bridge import UiBridgeError


def _win(**overrides) -> dict:
    base = {
        "title": "微信",
        "cls": "#32770",
        "pid": 4242,
        "proc": "Weixin",
        "x": 100,
        "y": 100,
        "w": 800,
        "h": 600,
        "minimized": False,
    }
    base.update(overrides)
    return base


class GuardUiTargetTest(unittest.TestCase):
    def test_normal_app_window_allowed(self):
        self.assertEqual(computer_use._guard_ui_target(_win()), "")

    def test_console_window_refused(self):
        for proc in ("cmd", "cmd.exe", "powershell", "pwsh", "WindowsTerminal"):
            error = computer_use._guard_ui_target(_win(proc=proc))
            self.assertIn("终端", error, proc)

    def test_registry_window_refused_by_title(self):
        error = computer_use._guard_ui_target(_win(title="注册表编辑器", proc="regedit"))
        self.assertIn("拒绝", error)

    def test_protected_process_refused(self):
        error = computer_use._guard_ui_target(_win(proc="lsass.exe", title="随便"))
        self.assertIn("系统关键进程", error)

    def test_own_process_refused(self):
        error = computer_use._guard_ui_target(_win(pid=os.getpid(), proc="python"))
        self.assertIn("自己", error)

    def test_invisible_window_refused(self):
        error = computer_use._guard_ui_target(_win(w=0, h=0))
        self.assertIn("不可见", error)

    def test_self_confirmation_page_refused(self):
        # 确认卡片在浏览器里的 Streamlit 页面上；自动化它 = 自己点“批准”
        error = computer_use._guard_ui_target(_win(title="Streamlit - Google Chrome", proc="chrome"))
        self.assertIn("确认页面", error)

    def test_unrelated_browser_window_allowed(self):
        self.assertEqual(
            computer_use._guard_ui_target(_win(title="哔哩哔哩 - Google Chrome", proc="chrome")), ""
        )

    def test_missing_info_refused(self):
        self.assertTrue(computer_use._guard_ui_target({}))
        self.assertTrue(computer_use._guard_ui_target(None))


class UiActionParamsTest(unittest.TestCase):
    def test_ui_windows_defaults(self):
        prepared, error = computer_use._prepare_arguments("ui_windows", {})
        self.assertEqual(error, "")
        self.assertEqual(prepared["limit"], computer_use._MAX_UI_WINDOWS)

    def test_ui_tree_caps_depth_and_limit(self):
        prepared, error = computer_use._prepare_arguments("ui_tree", {"depth": 99, "limit": 9999})
        self.assertEqual(error, "")
        self.assertEqual(prepared["depth"], 6)
        self.assertEqual(prepared["limit"], 300)

    def test_ui_click_requires_element_target(self):
        _prepared, error = computer_use._prepare_arguments("ui_click", {"window": "微信"})
        self.assertIn("name", error)

    def test_ui_set_text_requires_text(self):
        _prepared, error = computer_use._prepare_arguments(
            "ui_set_text", {"window": "微信", "name": "输入框"}
        )
        self.assertIn("text", error)

    def test_ui_set_text_accepts_named_control(self):
        prepared, error = computer_use._prepare_arguments(
            "ui_set_text", {"window": "微信", "name": "搜索", "text": "你好"}
        )
        self.assertEqual(error, "")
        self.assertEqual(prepared["text"], "你好")
        self.assertEqual(prepared["name"], "搜索")

    def test_ocr_region_accepts_string_and_list(self):
        prepared, error = computer_use._prepare_arguments("ocr_text", {"region": "[0, 0, 400, 300]"})
        self.assertEqual(error, "")
        self.assertEqual(prepared["region"], [0, 0, 400, 300])
        prepared, error = computer_use._prepare_arguments("ocr_text", {"region": [10, 20, 30, 40]})
        self.assertEqual(error, "")
        self.assertEqual(prepared["region"], [10, 20, 30, 40])

    def test_ocr_region_rejects_bad_shape(self):
        _prepared, error = computer_use._prepare_arguments("ocr_text", {"region": "[0, 0]"})
        self.assertIn("region", error)

    def test_click_text_index_is_clamped(self):
        prepared, error = computer_use._prepare_arguments(
            "click_text", {"text": "发送", "index": 99}
        )
        self.assertEqual(error, "")
        self.assertEqual(prepared["index"], 20)

    def test_ui_wait_timeout_is_capped(self):
        _prepared, error = computer_use._prepare_arguments("ui_wait", {"timeout": 999})
        self.assertIn("timeout", error)

    def test_window_param_is_truncated(self):
        prepared, error = computer_use._prepare_arguments(
            "ui_tree", {"window": "x" * 500}
        )
        self.assertEqual(error, "")
        self.assertEqual(len(prepared["window"]), 120)


class UiFormatTest(unittest.TestCase):
    def test_tree_hides_password_and_marks_state(self):
        data = {
            "window": {"title": "登录", "pid": 1, "proc": "App", "x": 0, "y": 0, "w": 100, "h": 100},
            "nodes": [
                {"i": 0, "name": "密码", "type": "Edit", "aid": "pwd", "x": 1, "y": 2, "w": 3, "h": 4,
                 "enabled": True, "offscreen": False, "password": True, "value": "hunter2"},
                {"i": 1, "name": "登录", "type": "Button", "aid": "", "x": 5, "y": 6, "w": 7, "h": 8,
                 "enabled": False, "offscreen": True, "password": False, "value": ""},
            ],
        }
        text = computer_use._format_ui_tree(data)
        self.assertIn("密码框", text)
        self.assertNotIn("hunter2", text)
        self.assertIn("disabled", text)
        self.assertIn("offscreen", text)

    def test_tree_is_truncated(self):
        nodes = [
            {"i": index, "name": "按钮" * 20, "type": "Button", "aid": "", "x": 0, "y": 0, "w": 1, "h": 1,
             "enabled": True, "offscreen": False, "password": False, "value": ""}
            for index in range(200)
        ]
        text = computer_use._format_ui_tree({"window": {}, "nodes": nodes})
        self.assertLessEqual(len(text), computer_use._MAX_UI_TREE_CHARS + 200)
        self.assertIn("已截断", text)

    def test_windows_mark_foreground(self):
        text = computer_use._format_ui_windows(
            [{"title": "记事本", "pid": 1, "proc": "notepad", "cls": "Notepad", "x": 0, "y": 0,
              "w": 10, "h": 10, "foreground": True}]
        )
        self.assertIn("[前台]", text)
        self.assertIn("notepad", text)


class OcrMappingTest(unittest.TestCase):
    def test_scale_and_offset(self):
        lines = [{"text": "你好", "x": 10, "y": 20, "w": 40, "h": 10}]
        mapped = computer_use._map_ocr_lines(lines, scale=2.0, offset_x=100, offset_y=200)
        self.assertEqual(mapped[0]["x"], 120)
        self.assertEqual(mapped[0]["y"], 240)
        self.assertEqual(mapped[0]["w"], 80)
        self.assertEqual(mapped[0]["cx"], 160)
        self.assertEqual(mapped[0]["cy"], 250)

    def test_empty_text_is_dropped(self):
        self.assertEqual(
            computer_use._map_ocr_lines([{"text": "  ", "x": 0, "y": 0, "w": 1, "h": 1}], 1.0, 0, 0),
            [],
        )


class OcrMatchTest(unittest.TestCase):
    LINES = [
        {"text": "发送消息", "cx": 10, "cy": 20},
        {"text": "取消", "cx": 30, "cy": 40},
        {"text": "发送文件", "cx": 50, "cy": 60},
    ]

    def test_match_is_case_insensitive_and_substring(self):
        line, error = computer_use._select_ocr_line(self.LINES, "发送")
        self.assertEqual(error, "")
        self.assertEqual(line["cx"], 10)

    def test_index_selects_nth_match(self):
        line, error = computer_use._select_ocr_line(self.LINES, "发送", index=1)
        self.assertEqual(error, "")
        self.assertEqual(line["cx"], 50)

    def test_no_match_reports_sample(self):
        line, error = computer_use._select_ocr_line(self.LINES, "确定")
        self.assertIsNone(line)
        self.assertIn("没有找到", error)

    def test_index_out_of_range(self):
        line, error = computer_use._select_ocr_line(self.LINES, "发送", index=5)
        self.assertIsNone(line)
        self.assertIn("超出范围", error)


class UiActionOrderTest(unittest.TestCase):
    """守卫必须在动作之前：终端窗口不能被 Invoke 出去。"""

    def test_click_on_console_window_never_invokes(self):
        with mock.patch.object(
            computer_use.ui_bridge, "available", return_value=True
        ), mock.patch.object(
            computer_use.ui_bridge, "find_window", return_value=_win(proc="cmd", title="C:\\Windows")
        ), mock.patch.object(
            computer_use.ui_bridge, "invoke_element", side_effect=AssertionError("不应该调用")
        ):
            result = computer_use.computer_use.invoke(
                {"action": "ui_click", "window": "C:\\Windows", "name": "确定"}
            )
        self.assertIn("拒绝", result)

    def test_set_text_reports_password_field_refusal(self):
        with mock.patch.object(
            computer_use.ui_bridge, "available", return_value=True
        ), mock.patch.object(
            computer_use.ui_bridge, "find_window", return_value=_win()
        ), mock.patch.object(
            computer_use.ui_bridge, "set_element_value", side_effect=UiBridgeError("refuse to write into a password field")
        ):
            result = computer_use.computer_use.invoke(
                {"action": "ui_set_text", "window": "微信", "name": "密码", "text": "x"}
            )
        self.assertIn("写入控件失败", result)
        self.assertIn("password", result)

    def test_missing_window_gives_actionable_hint(self):
        with mock.patch.object(
            computer_use.ui_bridge, "available", return_value=True
        ), mock.patch.object(
            computer_use.ui_bridge, "find_window", side_effect=UiBridgeError("window not found")
        ):
            result = computer_use.computer_use.invoke({"action": "ui_tree", "window": "__nope__"})
        self.assertIn("ui_windows", result)

    def test_ui_wait_does_not_leak_screen_content(self):
        tree = {
            "window": _win(),
            "nodes": [
                {"i": 0, "name": "登录", "type": "Button", "aid": "login",
                 "x": 111, "y": 222, "w": 80, "h": 30,
                 "enabled": True, "offscreen": False, "password": False, "value": ""}
            ],
        }
        with mock.patch.object(
            computer_use.ui_bridge, "available", return_value=True
        ), mock.patch.object(
            computer_use.ui_bridge, "find_window", return_value=_win()
        ), mock.patch.object(
            computer_use.ui_bridge, "dump_tree", return_value=tree
        ):
            result = computer_use.computer_use.invoke(
                {"action": "ui_wait", "window": "微信", "name": "登录", "timeout": 1}
            )
        self.assertIn("已等到控件", result)
        # 不回显坐标与窗口标题（它是免确认执行的读轮询）
        self.assertNotIn("111", result)
        self.assertNotIn("微信", result.split("。")[1] if "。" in result else "")

    def test_click_text_checks_window_before_capturing(self):
        """指定的目标窗口是终端时，不允许先截图再发现不能点。"""
        with mock.patch.object(
            computer_use.ui_bridge, "available", return_value=True
        ), mock.patch.object(
            computer_use.ui_bridge, "find_window", return_value=_win(proc="cmd", title="C:\\Windows")
        ), mock.patch.object(
            computer_use, "_capture_for_ocr", side_effect=AssertionError("不应该截图")
        ):
            result = computer_use.computer_use.invoke(
                {"action": "click_text", "window": "C:\\Windows", "text": "dir"}
            )
        self.assertIn("拒绝", result)

    def test_bridge_unavailable_reports_cleanly(self):
        with mock.patch.object(computer_use.ui_bridge, "available", return_value=False):
            result = computer_use.computer_use.invoke({"action": "ui_windows"})
        self.assertIn("不可用", result)


class UiPolicyTest(unittest.TestCase):
    def test_read_only_set_covers_sensitive_reads_but_not_clicks(self):
        for action in ("ui_windows", "ui_tree", "ui_wait", "ocr_text"):
            self.assertIn(action, policy.DESKTOP_READ_ONLY_ACTIONS)
        for action in ("ui_click", "ui_set_text", "click_text"):
            self.assertNotIn(action, policy.DESKTOP_READ_ONLY_ACTIONS)

    def test_click_text_and_ui_click_need_pin(self):
        for action in ("ui_click", "ui_set_text", "click_text"):
            self.assertEqual(
                policy.reversibility_of("computer_use", {"action": action}), "R2", action
            )
        self.assertEqual(
            policy.reversibility_of("computer_use", {"action": "ui_tree"}), "R0"
        )


if __name__ == "__main__":
    unittest.main()
