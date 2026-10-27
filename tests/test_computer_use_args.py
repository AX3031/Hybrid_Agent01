"""桌面控制工具的“列表参数被写成字符串”回归用例。

真实事故：模型把 hotkey 的 keys 发成 `'["alt", "tab"]'`（JSON 字符串），
旧实现按 `+`/空白切分，切出 `["alt",` 与 `"tab"]` 两个垃圾键名，
工具返回「Pressed」但实际没有按下 alt+tab，任务在原地打转。
"""

import unittest

from agent.tools import computer_use


class CoerceStrListTest(unittest.TestCase):
    def test_json_string_array_is_parsed(self):
        self.assertEqual(computer_use._normalize_keys('["alt", "tab"]'), ["alt", "tab"])

    def test_plain_hotkey_string_still_splits(self):
        self.assertEqual(computer_use._normalize_keys("alt+tab"), ["alt", "tab"])
        self.assertEqual(computer_use._normalize_keys("ctrl s"), ["ctrl", "s"])

    def test_real_list_is_preserved(self):
        self.assertEqual(computer_use._normalize_keys(["ctrl", "s"]), ["ctrl", "s"])

    def test_list_with_nested_json_string_is_flattened(self):
        self.assertEqual(computer_use._normalize_keys(['["alt", "tab"]']), ["alt", "tab"])

    def test_malformed_bracket_string_is_cleaned(self):
        # 不是合法 JSON：去掉方括号和引号后仍要得到干净键名
        self.assertEqual(computer_use._normalize_keys("[alt, tab]"), ["alt", "tab"])

    def test_empty_and_none(self):
        self.assertEqual(computer_use._normalize_keys(""), [])
        self.assertEqual(computer_use._normalize_keys("   "), [])
        self.assertIsNone(computer_use._normalize_keys(None))


class HotkeyArgumentsTest(unittest.TestCase):
    def test_hotkey_accepts_stringified_list(self):
        prepared, error = computer_use._prepare_arguments(
            "hotkey", {"keys": '["alt", "tab"]'}
        )
        self.assertEqual(error, "")
        self.assertEqual(prepared, {"keys": ["alt", "tab"]})

    def test_hotkey_rejects_garbage_after_cleanup(self):
        _prepared, error = computer_use._prepare_arguments("hotkey", {"keys": "   "})
        self.assertIn("不能为空", error)

    def test_app_open_arguments_accepts_stringified_list(self):
        prepared, error = computer_use._prepare_arguments(
            "app_open", {"name": "WeChat", "arguments": '["--minimized"]'}
        )
        self.assertEqual(error, "")
        self.assertEqual(prepared["arguments"], ["--minimized"])

    def test_app_open_arguments_still_blocks_dangerous_chars(self):
        _prepared, error = computer_use._prepare_arguments(
            "app_open", {"name": "WeChat", "arguments": '["&calc&"]'}
        )
        self.assertIn("拒绝", error)

    def test_screenshot_region_accepts_stringified_list(self):
        prepared, error = computer_use._prepare_arguments(
            "screenshot", {"region": "[0, 0, 800, 600]"}
        )
        self.assertEqual(error, "")
        self.assertEqual(prepared["region"], [0, 0, 800, 600])

    def test_screenshot_region_rejects_wrong_length(self):
        _prepared, error = computer_use._prepare_arguments("screenshot", {"region": "[0, 0]"})
        self.assertIn("region", error)


if __name__ == "__main__":
    unittest.main()
