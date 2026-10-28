"""任务技能库用例：步骤脱敏、截图复制、保存/读取/删除。"""

import json
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


class SkillLibraryV2Test(unittest.TestCase):
    """技能库 schema v2：按子目标存、按目标匹配、带建议动作与核对项。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agent_skill_v2_")).resolve()
        self.skill_dir = self.tmp / "skills"
        self.skill_dir.mkdir(parents=True)
        self.env = mock.patch.dict(os.environ, {"AGENT_SKILL_DIR": str(self.skill_dir)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def _write_skills(self, payload) -> None:
        (self.skill_dir / "skills.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def test_legacy_skill_is_migrated(self):
        self._write_skills(
            [
                {
                    "id": "old1",
                    "name": "打开微信",
                    "description": "需要打开微信时",
                    "steps": ["1. 启动应用：微信"],
                    "task_card": {},
                    "use_count": 3,
                }
            ]
        )
        saved = skill_library.list_skills()[0]
        self.assertEqual(saved["schema"], 2)
        self.assertEqual(saved["goal_match"], ["打开微信"])
        self.assertEqual(saved["intents"], [])
        self.assertEqual(saved["verify"], [])

    def test_bad_fields_do_not_crash(self):
        self._write_skills(
            [
                {
                    "id": "bad1",
                    "name": "  奇怪技能  ",
                    "steps": "第1步\n\n第2步",
                    "goal_match": ["", "  ", "别名", "别名"],
                    "intents": [None, {}, {"kind": "click", "target": "x" * 500}],
                    "verify": ["", "exists|a"],
                }
            ]
        )
        saved = skill_library.list_skills()[0]
        self.assertEqual(saved["name"], "奇怪技能")
        self.assertEqual(saved["steps"], ["第1步", "第2步"])
        self.assertEqual(saved["goal_match"], ["别名"])
        self.assertEqual(len(saved["intents"]), 1)
        self.assertLessEqual(len(saved["intents"][0]["target"]), 120)
        self.assertEqual(saved["verify"], ["exists|a"])

    def test_match_by_containment_alias_and_negative(self):
        skill_library.save_skill(
            name="打开微信", goal_match=["打开微信", "wechat"], steps=["1. 打开微信"]
        )
        skill_library.save_skill(name="导出报表", steps=["1. 导出"])

        hits = skill_library.match_skills("帮我打开微信并登录")
        self.assertEqual([item["name"] for item in hits], ["打开微信"])
        self.assertAlmostEqual(hits[0]["match_score"], 1.0)

        # 英文别名命中
        self.assertEqual([item["name"] for item in skill_library.match_skills("open wechat now")], ["打开微信"])
        # 不相关目标不推荐
        self.assertEqual(skill_library.match_skills("订一张去上海的机票"), [])

    def test_match_respects_limit(self):
        for name in ("打开微信", "打开微信聊天", "打开微信设置"):
            skill_library.save_skill(name=name, steps=["1. 打开"])
        hits = skill_library.match_skills("打开微信", limit=2)
        self.assertEqual(len(hits), 2)
        self.assertTrue(all(item["match_score"] >= 0.34 for item in hits))

    def test_description_only_mention_does_not_match(self):
        """描述里顺带提到目标词不能算命中（否则“从开始菜单打开软件”会被“打开微信”误命中）。"""
        skill_library.save_skill(
            name="从开始菜单打开软件",
            description="被要求打开微信、QQ 等桌面软件且 app_open 打不开时使用",
            steps=["1. 从开始菜单启动"],
        )
        self.assertEqual(skill_library.match_skills("打开微信"), [])
        # 但目标里真的写了技能名时仍能命中
        self.assertEqual(
            [item["name"] for item in skill_library.match_skills("用从开始菜单打开软件的方式打开微信")],
            ["从开始菜单打开软件"],
        )

    OPS = [
        {"name": "computer_use", "args": {"action": "app_open", "name": "微信"}, "status": "success"},
        {"name": "computer_use", "args": {"action": "ui_tree", "window": "微信"}, "status": "success"},
        {"name": "computer_use", "args": {"action": "ui_click", "window": "微信", "name": "登录"}, "status": "success"},
        {"name": "computer_use", "args": {"action": "ui_set_text", "window": "微信", "name": "搜索", "text": "你好"}, "status": "success"},
    ]

    def test_split_candidates_by_subgoals(self):
        candidates = skill_library.split_skill_candidates("打开微信，然后给邓三三发个你好", self.OPS)
        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0]["name"], "打开微信")
        self.assertIn("邓三三", candidates[1]["name"])
        for item in candidates:
            self.assertTrue(item["steps"])
            self.assertTrue(item["goal_match"])
        # 第一个子目标只包含启动/观察，不应把“点击/输入”算进去
        self.assertEqual([intent["kind"] for intent in candidates[0]["intents"]], ["app_open"])
        self.assertIn("click", [intent["kind"] for intent in candidates[1]["intents"]])

    def test_split_single_goal_returns_one_candidate(self):
        candidates = skill_library.split_skill_candidates("打开微信", self.OPS)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["name"], "打开微信")

    def test_split_without_operations_is_safe(self):
        candidates = skill_library.split_skill_candidates("打开微信，然后发消息", [])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["steps"], [])

    def test_intents_skip_observation_actions(self):
        intents = skill_library.intents_from_operations(self.OPS)
        kinds = [item["kind"] for item in intents]
        self.assertIn("app_open", kinds)
        self.assertIn("click", kinds)
        self.assertIn("set_text", kinds)
        # 观察类（ui_tree/ocr_text/screenshot）不产生建议动作
        self.assertNotIn("read_ui", kinds)
        self.assertEqual(len(kinds), len(set(kinds)))

    def test_derive_verify_from_file_operations(self):
        ops = [
            {"name": "write_text_file", "args": {"path": "C:/out/a.txt"}, "status": "success"},
            {"name": "write_text_file", "args": {"path": "C:/out/a.txt"}, "status": "success"},
            {"name": "create_directory", "args": {"path": "C:/out"}, "status": "error"},
            {"name": "copy_path", "args": {"destination": "C:/out/b.txt"}, "status": "success"},
        ]
        checks = skill_library.derive_verify(ops)
        self.assertEqual(checks, ["exists|C:/out/a.txt", "exists|C:/out/b.txt"])

    def test_editing_steps_keeps_goals_intents_verify(self):
        saved = skill_library.save_skill(
            name="打开微信",
            steps=["1. 打开"],
            goal_match=["打开微信", "wechat"],
            intents=[{"kind": "app_open", "target": "微信"}],
            verify=["exists|C:/out/a.txt"],
        )
        updated = skill_library.save_skill(name="打开微信", steps=["1. 新步骤"])
        self.assertEqual(updated["id"], saved["id"])
        self.assertEqual(updated["goal_match"], ["打开微信", "wechat"])
        self.assertEqual(updated["intents"], [{"kind": "app_open", "target": "微信"}])
        self.assertEqual(updated["verify"], ["exists|C:/out/a.txt"])

    def test_reference_lines_and_text(self):
        saved = skill_library.save_skill(
            name="打开微信",
            steps=["1. 启动应用：微信"],
            intents=[{"kind": "app_open", "target": "微信"}],
            verify=["exists|C:/out/a.txt"],
        )
        lines = skill_library.reference_lines(saved)
        self.assertIn("1. 启动应用：微信", lines)
        self.assertTrue(any("建议动作" in line for line in lines))
        self.assertTrue(any("完成后核对" in line for line in lines))
        text = skill_library.skill_to_text(saved)
        self.assertIn("建议动作", text)


if __name__ == "__main__":
    unittest.main()
