"""任务技能库用例：步骤脱敏、截图复制、保存/读取/删除。"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent import skill_library


class SkillLibraryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agent_skill_")).resolve()
        self.skill_dir = self.tmp / "skills"
        self.env = mock.patch.dict(
            os.environ,
            {"AGENT_SKILL_DIR": str(self.skill_dir)},
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_screenshot(self, name: str = "shot.png") -> Path:
        path = self.tmp / name
        path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        return path

    def test_steps_are_human_readable_and_do_not_keep_typed_secret(self):
        operations = [
            {
                "name": "computer_use",
                "args": {"action": "type_text", "text": "my-secret-token"},
                "status": "success",
            },
            {
                "name": "run_command",
                "args": {"command": "echo my-secret-token"},
                "status": "success",
            },
        ]
        steps = skill_library.steps_from_operations(operations)
        text = "\n".join(steps)
        self.assertIn("输入文字", text)
        self.assertIn("命令内容已省略", text)
        self.assertNotIn("my-secret-token", text)
        self.assertIn("已脱敏", text)

    def test_save_get_list_and_copy_screenshots(self):
        shot = self._make_screenshot()
        operations = [
            {
                "name": "computer_use",
                "args": {"action": "screenshot"},
                "status": "success",
                "result": f"截图文件：{shot}",
            }
        ]
        candidates = skill_library.screenshot_candidates(operations)
        self.assertEqual(candidates, [str(shot)])

        saved = skill_library.save_skill(
            name="导出报表",
            description="内部系统导出",
            steps=["1. 打开应用", "2. 点击导出"],
            key_screenshots=candidates,
            final_screenshots=candidates,
            task_card={
                "allowed_actions": ["读", "桌面"],
                "output_location": str(self.tmp),
                "max_steps": 12,
            },
        )
        self.assertEqual(saved["name"], "导出报表")
        self.assertTrue(saved["key_screenshots"])
        self.assertTrue(Path(saved["key_screenshots"][0]).is_file())
        self.assertTrue(Path(saved["final_screenshots"][0]).is_file())
        self.assertEqual(
            skill_library.get_skill(saved["id"])["task_card"]["max_steps"],
            12,
        )
        self.assertEqual(len(skill_library.list_skills()), 1)

        updated = skill_library.save_skill(
            name="导出报表（更新）",
            skill_id=saved["id"],
            steps=["1. 更新步骤"],
            task_card={},
        )
        self.assertEqual(updated["id"], saved["id"])
        self.assertEqual(updated["created_at"], saved["created_at"])
        self.assertEqual(len(skill_library.list_skills()), 1)

        self.assertTrue(skill_library.delete_skill(saved["id"]))
        self.assertEqual(skill_library.list_skills(), [])

    def test_empty_skill_name_is_rejected(self):
        with self.assertRaises(ValueError):
            skill_library.save_skill(name="  ")

    def test_edit_reuses_existing_screenshot_without_recopying(self):
        """编辑技能时回传技能自己的截图绝对路径，不应再复制一份。"""
        shot = self._make_screenshot("orig.png")
        saved = skill_library.save_skill(
            name="带图技能",
            steps=["1. 打开"],
            key_screenshots=[str(shot)],
        )
        # 技能库内已有一份副本
        first_copy = Path(saved["key_screenshots"][0])
        self.assertTrue(first_copy.is_file())
        before = set((self.skill_dir / "screenshots" / saved["id"]).iterdir())

        # 用绝对路径再保存一次（模拟“编辑此技能”），不应新增文件
        updated = skill_library.save_skill(
            name="带图技能",
            skill_id=saved["id"],
            steps=["1. 打开（改）"],
            key_screenshots=[str(first_copy)],
        )
        after = set((self.skill_dir / "screenshots" / saved["id"]).iterdir())
        self.assertEqual(before, after, "编辑不应重复复制截图")
        self.assertEqual(
            Path(updated["key_screenshots"][0]).name,
            first_copy.name,
        )

    def test_same_name_save_updates_instead_of_duplicating(self):
        """未传 skill_id 时同名保存应更新而不是新建，避免“只能存一个/重复堆叠”。"""
        first = skill_library.save_skill(name="打开浏览器", steps=["1. 旧步骤"])
        second = skill_library.save_skill(name="打开浏览器", steps=["1. 新步骤"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(skill_library.list_skills()), 1)
        self.assertEqual(second["steps"], ["1. 新步骤"])

        # 不同名仍新建
        other = skill_library.save_skill(name="导出报表", steps=["1. 导出"])
        self.assertNotEqual(other["id"], first["id"])
        self.assertEqual(len(skill_library.list_skills()), 2)

    def test_mark_skill_used_increments_count(self):
        saved = skill_library.save_skill(name="打开计算器", steps=["1. 打开"])
        skill_library.mark_skill_used(saved["id"])
        self.assertEqual(skill_library.get_skill(saved["id"])["use_count"], 1)


if __name__ == "__main__":
    unittest.main()
