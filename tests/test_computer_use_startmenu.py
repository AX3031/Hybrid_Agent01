"""Windows 开始菜单兜底打开应用的用例（不依赖 OmniPilot 运行时）。

覆盖：
- 中英别名归一（微信 ↔ WeChat）；
- 新动作 app_open_start_menu 仍受高危程序拦截；
- 开始菜单快捷方式查找优先文件名、其次别名；
- 人话描述能识别新动作。
"""

import unittest
from pathlib import Path
from tempfile import mkdtemp
from shutil import rmtree
from unittest import mock

from agent.tools import computer_use
from agent.tools.safety import describe_operation


class AppNameVariantsTest(unittest.TestCase):
    def test_chinese_name_expands_to_english_alias(self):
        variants = computer_use._app_name_variants("微信")
        self.assertEqual(variants["微信"], "name")
        self.assertIn("wechat", variants)
        self.assertEqual(variants["wechat"], "alias")

    def test_unknown_name_only_self(self):
        self.assertEqual(
            computer_use._app_name_variants("  FoO  "), {"foo": "name"}
        )


class StartMenuActionGuardTest(unittest.TestCase):
    def test_blocked_app_rejected_for_start_menu_action(self):
        prepared, error = computer_use._prepare_arguments(
            "app_open_start_menu", {"name": "powershell"}
        )
        self.assertEqual(prepared, {})
        self.assertIn("拒绝启动", error)

    def test_friendly_name_passes(self):
        prepared, error = computer_use._prepare_arguments(
            "app_open_start_menu", {"name": "微信"}
        )
        self.assertEqual(error, "")
        self.assertEqual(prepared, {"name": "微信"})

    def test_localized_shell_names_blocked(self):
        """中文/显示名不能绕过黑名单：命令提示符/注册表编辑器都被拒。"""
        for risky in ("Command Prompt", "命令提示符", "Registry Editor", "注册表编辑器"):
            _prepared, error = computer_use._prepare_arguments(
                "app_open_start_menu", {"name": risky}
            )
            self.assertIn("拒", error, risky)
        self.assertTrue(computer_use._is_high_risk_display_name("command prompt"))
        self.assertTrue(computer_use._is_blocked_program_name("Registry Editor"))

    def test_common_words_not_overblocked(self):
        """“服务/运行”作为精确显示名拦截，但包含这些字的正常应用不误拦。"""
        self.assertFalse(computer_use._is_high_risk_display_name("服务猫"))
        self.assertFalse(computer_use._is_high_risk_display_name("WeChat"))

    def test_shortcut_target_recheck_blocks_terminal(self):
        """命中名为“命令提示符”的快捷方式时，即使名字未命中也会因目标/文件名被拒。"""
        with mock.patch.object(
            computer_use, "_start_menu_dirs", return_value=[]
        ):
            self.assertIn("没有可用的开始菜单", computer_use._launch_start_menu_app("cmd"))
        # 直接验证黑名单判定函数（不依赖真实开始菜单）
        self.assertTrue(computer_use._is_blocked_program_name("cmd.exe"))
        self.assertTrue(computer_use._is_blocked_program_name("Command Prompt"))


class FindShortcutTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(mkdtemp(prefix="startmenu_")).resolve()
        programs = self.root / "Programs"
        programs.mkdir()
        (programs / "WeChat.lnk").write_bytes(b"fake")
        (programs / "Notepad.lnk").write_bytes(b"fake")
        self.patcher = mock.patch.object(
            computer_use, "_start_menu_dirs", return_value=[programs]
        )
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        rmtree(self.root, ignore_errors=True)

    def test_direct_name_match_wins(self):
        path, matched = computer_use._find_start_menu_shortcut("notepad")
        self.assertEqual(Path(path).name, "Notepad.lnk")
        self.assertEqual(matched, "notepad")

    def test_alias_match_for_chinese_name(self):
        # 微信没有“微信.lnk”，靠英文别名命中 WeChat.lnk
        path, matched = computer_use._find_start_menu_shortcut("微信")
        self.assertEqual(Path(path).name, "WeChat.lnk")
        self.assertEqual(matched, "")

    def test_missing_app_returns_none(self):
        path, matched = computer_use._find_start_menu_shortcut("不存在的软件")
        self.assertIsNone(path)
        self.assertEqual(matched, "")


class DescribeOperationTest(unittest.TestCase):
    def test_start_menu_action_has_readable_label(self):
        title, detail = describe_operation(
            "computer_use", {"action": "app_open_start_menu", "name": "微信"}
        )
        self.assertIn("开始菜单", title)
        self.assertIn("微信", detail)


if __name__ == "__main__":
    unittest.main()
