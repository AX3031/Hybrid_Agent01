"""文档一致性测试：防止 README/架构文档里的数字与代码漂移。

策略：

- `docs/_generated/facts.md` 由 `scripts/doc_facts.py` 从代码/配置派生；
- 本测试先校验这个生成文件是最新的；
- 再校验 README/PROJECT_OVERVIEW/ARCHITECTURE/OS_TOOLS 中仍手写的
  关键数字与生成事实一致；
- 最后检查工具/动作是否被文档覆盖、Markdown 本地链接是否还存在。

历史文档（安全报告、阶段复盘、CHANGELOG）里的数字是当时快照，不在
“必须与当前代码一致”的范围内；它们会通过 `facts.md` 的说明指向当前值。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from urllib.parse import unquote

from scripts import doc_facts

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
OVERVIEW = ROOT / "docs" / "PROJECT_OVERVIEW.md"
ARCHITECTURE = ROOT / "docs" / "ARCHITECTURE.md"
OS_TOOLS = ROOT / "docs" / "OS_TOOLS.md"
AGENTS = ROOT / "AGENTS.md"

LIVING_DOCS = (README, OVERVIEW, ARCHITECTURE, OS_TOOLS)

# 明确已经过时、且只应出现在历史文档里的字符串。
STALE_STRINGS = (
    "28 条危险命令",
    "28 条正则黑名单",
    "28 条规则",
    "18 个动作",
    "193 条",
    "`qwen3.5-flash`（对话",
    "`glm-5`（备用",
)

LINK_PATTERN = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class DocsFactsTest(unittest.TestCase):
    """所有断言都只读文件，不联网、不调用模型。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.facts = doc_facts.collect_facts()

    def test_generated_facts_file_is_current(self):
        ok, message = doc_facts.check_facts_file()
        self.assertTrue(ok, message)

    def test_readme_matches_facts(self):
        text = _read(README)
        self.assertIn("docs/_generated/facts.md", text, "README 应链接自动生成的事实页")
        self.assertIn(f"{self.facts['tool_total']} 个工具", text)
        self.assertIn(f"{self.facts['action_total']} 个动作", text)
        self.assertIn(
            f"普通任务默认 {self.facts['task_steps']} 步 / {self.facts['task_minutes']} 分钟 / {self.facts['task_cost']:g} 元",
            text,
        )
        self.assertIn(
            f"桌面任务默认 {self.facts['desktop_steps']} 步 / {self.facts['desktop_minutes']} 分钟 / {self.facts['desktop_cost']:g} 元",
            text,
        )
        self.assertIn(f"`{self.facts['model_main']}`", text)
        self.assertIn(f"`{self.facts['model_backup']}`", text)

    def test_project_overview_matches_facts(self):
        text = _read(OVERVIEW)
        self.assertIn("_generated/facts.md", text, "PROJECT_OVERVIEW 应链接自动生成的事实页")
        self.assertIn(
            f"模型：`{self.facts['model_main']}`（对话）、"
            f"`{self.facts['model_backup']}`（备用对话模型）",
            text,
        )
        self.assertIn(f"{self.facts['danger_rules']} 条危险命令黑名单", text)
        self.assertIn(
            f"普通任务 {self.facts['task_steps']} 步/{self.facts['task_minutes']} 分钟/{self.facts['task_cost']:g} 元",
            text,
        )
        self.assertIn(
            f"桌面任务 {self.facts['desktop_steps']} 步/{self.facts['desktop_minutes']} 分钟/{self.facts['desktop_cost']:g} 元",
            text,
        )
        self.assertIn(
            f"`os_tools`（{self.facts['tool_os']} 个）+ "
            f"`agent_tools`（{self.facts['tool_business']} 个）+ "
            f"`computer_use`（1 个，{self.facts['action_total']} 动作）",
            text,
        )

    def test_architecture_matches_facts(self):
        text = _read(ARCHITECTURE)
        self.assertIn("_generated/facts.md", text, "ARCHITECTURE 应链接自动生成的事实页")
        self.assertIn(
            f"{self.facts['tool_total']} 个 `@tool` 函数",
            text,
        )
        self.assertIn(f"{self.facts['danger_rules']} 条正则黑名单", text)
        self.assertIn(
            f"普通任务默认 {self.facts['task_steps']} 步/{self.facts['task_minutes']} 分钟/{self.facts['task_cost']:g} 元",
            text,
        )
        self.assertIn(
            f"桌面任务默认 {self.facts['desktop_steps']} 步/{self.facts['desktop_minutes']} 分钟/{self.facts['desktop_cost']:g} 元",
            text,
        )
        self.assertIn(f"| `chat_model_name` | `{self.facts['model_main']}` |", text)
        self.assertIn(
            f"| `backup_chat_model_name` | `{self.facts['model_backup']}` |", text
        )

    def test_os_tools_matches_facts(self):
        text = _read(OS_TOOLS)
        self.assertIn("_generated/facts.md", text, "OS_TOOLS 应链接自动生成的事实页")
        self.assertIn(f"一个工具 {self.facts['action_total']} 个动作", text)
        self.assertIn(
            f"{self.facts['action_total']} 个动作（其中 {self.facts['action_ui']} 个是控件级/OCR）",
            text,
        )
        self.assertIn(f"{self.facts['danger_rules']} 条规则", text)
        self.assertIn(
            f"普通任务默认最多 **{self.facts['task_steps']} 次工具调用 / "
            f"{self.facts['task_minutes']} 分钟 / {self.facts['task_cost']:g} 元**",
            text,
        )
        self.assertIn(
            f"桌面任务默认 **{self.facts['desktop_steps']} 次 / "
            f"{self.facts['desktop_minutes']} 分钟 / {self.facts['desktop_cost']:g} 元**",
            text,
        )

    def test_no_stale_numbers_in_living_docs(self):
        for path in LIVING_DOCS:
            text = _read(path)
            for stale in STALE_STRINGS:
                with self.subTest(path=path.name, stale=stale):
                    self.assertNotIn(
                        stale,
                        text,
                        f"{path.relative_to(ROOT)} 仍出现过时数字/模型名：{stale}；"
                        "请改为当前事实或链接 docs/_generated/facts.md",
                    )

    def test_all_registered_tools_are_documented(self):
        corpus = "\n".join(_read(path) for path in (README, OVERVIEW, ARCHITECTURE, OS_TOOLS))
        missing = [name for name in self.facts["tool_name_list"] if name not in corpus]
        self.assertEqual(
            missing,
            [],
            f"以下注册工具没有出现在 README/OVERVIEW/ARCHITECTURE/OS_TOOLS：{missing}",
        )

    def test_all_desktop_actions_are_documented(self):
        text = _read(OS_TOOLS)
        missing = [
            action for action in self.facts["action_name_list"] if action not in text
        ]
        self.assertEqual(
            missing,
            [],
            f"以下桌面动作没有出现在 docs/OS_TOOLS.md：{missing}",
        )

    def test_local_markdown_links_exist(self):
        files = [README, AGENTS, *sorted((ROOT / "docs").rglob("*.md"))]
        broken: list[str] = []
        for path in files:
            for match in LINK_PATTERN.finditer(_read(path)):
                raw = match.group(1).strip()
                if raw.startswith(("http://", "https://", "mailto:", "#", "<")):
                    continue
                target = unquote(raw.split("#", 1)[0])
                if not target:
                    continue
                resolved = (path.parent / target).resolve()
                if not resolved.exists():
                    broken.append(
                        f"{path.relative_to(ROOT)} -> {raw}"
                    )
        self.assertEqual(broken, [], "存在失效的本地 Markdown 链接：\n" + "\n".join(broken))


if __name__ == "__main__":
    unittest.main()
