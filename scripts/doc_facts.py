"""从代码与配置生成“当前事实”，供文档同步脚本和一致性测试使用。

设计原则：

- **只派生确定性事实**：工具数、动作数、用例数、预算、模型名、规则条数、
  默认工作区等；不生成“为什么”叙事，也不改历史快照；
- **唯一入口**：`docs/_generated/facts.md` 由本模块渲染，`scripts/sync_doc_facts.py`
  负责写入；`tests/test_docs_consistency.py` 负责比对；
- **不联网、不调用模型**：只 import 本地模块、读 YAML、发现 unittest 用例。
"""

from __future__ import annotations

import sys
import unittest
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.guard.config import (  # noqa: E402
    desktop_task_defaults,
    keep_recent_screenshots,
    task_defaults,
)
from agent.guard.criteria import _SUPPORTED as MACHINE_CHECK_KINDS  # noqa: E402
from agent.guard.repeat_guard import _HARD_REPEATS, _SOFT_REPEATS  # noqa: E402
from agent.guard.task_card import (  # noqa: E402
    ALL_ACTIONS,
    DEFAULT_FORBIDDEN,
    QUICK_FORBIDDEN,
)
from agent.tool_registry import (  # noqa: E402
    AGENT_TOOLS,
    BUSINESS_TOOLS,
    CONFIRM_TOOLS,
    DESKTOP_TOOLS,
    OS_TOOLS,
    TOOL_CALL_LIMIT,
)
from agent.tools.computer_use import (  # noqa: E402
    READ_ONLY_ACTIONS,
    _ACTION_PARAMS,
    _BLOCKED_APPS,
    _PROTECTED_PROCESSES,
    _UI_ACTIONS,
    action_requires_confirmation,
)
from agent.tools.safety import _BLOCKED_WRITE_SUFFIXES, _DANGEROUS_COMMANDS  # noqa: E402
from utils.config_handler import model_conf  # noqa: E402

FACTS_PATH = ROOT / "docs" / "_generated" / "facts.md"
GENERATED_MARKER = "<!-- 由 scripts/sync_doc_facts.py 生成；不要手改。 -->"


@lru_cache(maxsize=1)
def count_tests() -> tuple[int, int]:
    """返回（全部用例数, P0 守卫层用例数）。

    与 `scripts/verify_small_business.py` 保持同一口径：P0 = 测试 id 里包含
    `test_small_business_guard` 的用例。这里只发现、不执行。
    """
    suite = unittest.defaultTestLoader.discover(
        start_dir=str(ROOT / "tests"),
        top_level_dir=str(ROOT),
    )

    def _ids(item):
        if isinstance(item, unittest.TestSuite):
            for child in item:
                yield from _ids(child)
        else:
            yield item.id()

    ids = list(_ids(suite))
    return len(ids), sum(1 for item_id in ids if "test_small_business_guard" in item_id)


def collect_facts() -> dict:
    """读取代码/配置，返回一份可渲染的确定性事实。"""
    total_tests, p0_tests = count_tests()
    task = task_defaults()
    desktop = desktop_task_defaults()

    return {
        "tool_total": len(AGENT_TOOLS),
        "tool_os": len(OS_TOOLS),
        "tool_business": len(BUSINESS_TOOLS),
        "tool_desktop": len(DESKTOP_TOOLS),
        "tool_confirm": len(CONFIRM_TOOLS),
        "action_total": len(_ACTION_PARAMS),
        "action_direct": len(READ_ONLY_ACTIONS),
        "action_confirm": sum(
            1 for action in _ACTION_PARAMS if action_requires_confirmation(action)
        ),
        "action_ui": len(_UI_ACTIONS),
        "danger_rules": len(_DANGEROUS_COMMANDS),
        "write_suffixes": len(_BLOCKED_WRITE_SUFFIXES),
        "blocked_apps": len(_BLOCKED_APPS),
        "protected_processes": len(_PROTECTED_PROCESSES),
        "test_total": total_tests,
        "test_p0": p0_tests,
        "model_main": str(model_conf.get("chat_model_name") or ""),
        "model_backup": str(model_conf.get("backup_chat_model_name") or ""),
        "task_steps": int(task["max_steps"]),
        "task_minutes": int(task["max_minutes"]),
        "task_cost": float(task["max_cost_yuan"]),
        "desktop_steps": int(desktop["max_steps"]),
        "desktop_minutes": int(desktop["max_minutes"]),
        "desktop_cost": float(desktop["max_cost_yuan"]),
        "tool_call_limit": int(TOOL_CALL_LIMIT),
        "repeat_soft": int(_SOFT_REPEATS),
        "repeat_hard": int(_HARD_REPEATS),
        "keep_screenshots": int(keep_recent_screenshots()),
        "machine_checks": "、".join(sorted(MACHINE_CHECK_KINDS)),
        "all_actions": "、".join(f"`{item}`" for item in ALL_ACTIONS),
        "default_forbidden": "、".join(DEFAULT_FORBIDDEN),
        "quick_forbidden": "、".join(QUICK_FORBIDDEN),
        "tool_names": "、".join(f"`{tool.name}`" for tool in AGENT_TOOLS),
        "confirm_names": "、".join(f"`{name}`" for name in sorted(CONFIRM_TOOLS)),
        "action_names": "、".join(f"`{name}`" for name in _ACTION_PARAMS),
        "direct_action_names": "、".join(
            f"`{name}`" for name in sorted(READ_ONLY_ACTIONS)
        ),
        "ui_action_names": "、".join(f"`{name}`" for name in sorted(_UI_ACTIONS)),
        # 列表版本给测试用，避免测试再解析 Markdown 字符串。
        "tool_name_list": [tool.name for tool in AGENT_TOOLS],
        "confirm_name_list": sorted(CONFIRM_TOOLS),
        "action_name_list": list(_ACTION_PARAMS),
        "direct_action_list": sorted(READ_ONLY_ACTIONS),
        "ui_action_list": sorted(_UI_ACTIONS),
    }


def render_facts_markdown() -> str:
    """把事实渲染成 Markdown；输出必须稳定，方便测试逐字比对。"""
    facts = collect_facts()
    lines = [
        GENERATED_MARKER,
        "# 当前代码事实（自动生成）",
        "",
        "> 本页是工具、动作、用例、预算、模型等“数字”的唯一事实源；README/架构文档只保留“为什么”。",
        "> 重新生成：`uv run python scripts/sync_doc_facts.py`；只校验：`uv run python scripts/sync_doc_facts.py --check`。",
        "> 安全报告、阶段复盘、CHANGELOG 等历史文档里的数字是当时快照，不随本页变化。",
        "",
        "## 1. 工具与动作",
        "",
        "| 项 | 当前值 |",
        "| --- | --- |",
        f"| 注册工具 | {facts['tool_total']} 个（电脑操作 {facts['tool_os']} + 业务 {facts['tool_business']} + 桌面控制 {facts['tool_desktop']}） |",
        f"| 需要确认的工具 | {facts['tool_confirm']} 个 |",
        f"| `computer_use` 动作 | {facts['action_total']} 个（直接放行 {facts['action_direct']} 个，需确认 {facts['action_confirm']} 个，控件级/OCR {facts['action_ui']} 个） |",
        "",
        "## 2. 规则与预算",
        "",
        "| 项 | 当前值 |",
        "| --- | --- |",
        f"| 危险命令规则 | {facts['danger_rules']} 条 |",
        f"| 可执行写入后缀 | {facts['write_suffixes']} 种 |",
        f"| 桌面应用黑名单 | {facts['blocked_apps']} 个条目 |",
        f"| 系统关键进程 | {facts['protected_processes']} 个 |",
        f"| 普通任务预算 | {facts['task_steps']} 步 / {facts['task_minutes']} 分钟 / {facts['task_cost']:g} 元 |",
        f"| 桌面任务预算 | {facts['desktop_steps']} 步 / {facts['desktop_minutes']} 分钟 / {facts['desktop_cost']:g} 元 |",
        f"| 工具调用硬上限 | {facts['tool_call_limit']} 步/单次运行 |",
        f"| 原地打转阈值 | 第 {facts['repeat_soft']} 次提醒 / 第 {facts['repeat_hard']} 次拒绝 |",
        f"| 发给模型的最近截图 | {facts['keep_screenshots']} 张 |",
        "",
        "## 3. 默认配置与模型",
        "",
        "| 项 | 当前值 |",
        "| --- | --- |",
        f"| 主模型 / 备用模型 | `{facts['model_main']}` / `{facts['model_backup']}` |",
        "| 默认工作区 | 由 `config/agent.yml` 的 `workspace_roots` 决定（本页不落具体路径） |",
        "| 默认只读 | 由 `config/agent.yml` 的 `read_only` 决定（本页不落具体值） |",
        f"| 机器判据类型 | {facts['machine_checks']} |",
        f"| 任务卡动作类别 | {facts['all_actions']} |",
        f"| 默认禁止动作 | {facts['default_forbidden']} |",
        f"| 快速卡默认禁止 | {facts['quick_forbidden']} |",
        "",
        "## 4. 离线用例",
        "",
        "| 项 | 当前值 |",
        "| --- | --- |",
        f"| 全部用例 | {facts['test_total']} 条 |",
        f"| P0 守卫层 | {facts['test_p0']} 条 |",
        "",
        "## 5. 注册工具清单",
        "",
        facts["tool_names"],
        "",
        "### 需要确认的工具",
        "",
        facts["confirm_names"],
        "",
        "## 6. 桌面动作清单",
        "",
        facts["action_names"],
        "",
        "### 直接放行",
        "",
        facts["direct_action_names"],
        "",
        "### 控件级/OCR",
        "",
        facts["ui_action_names"],
        "",
    ]
    return "\n".join(lines)


def check_facts_file() -> tuple[bool, str]:
    """返回（是否与生成内容一致, 说明）。文件缺失也算不一致。"""
    expected = render_facts_markdown()
    if not FACTS_PATH.exists():
        return False, f"缺少生成文件：{FACTS_PATH.relative_to(ROOT)}"
    # Git 在 Windows 上可能把 LF 转成 CRLF；比较前统一换行，避免误报。
    actual = FACTS_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")
    if actual == expected:
        return True, "docs/_generated/facts.md 与代码一致"
    return False, (
        "docs/_generated/facts.md 与代码不一致；"
        "请运行 `uv run python scripts/sync_doc_facts.py` 重新生成"
    )


def write_facts_file() -> Path:
    """把生成内容写入 docs/_generated/facts.md，返回路径。"""
    FACTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    FACTS_PATH.write_text(render_facts_markdown(), encoding="utf-8")
    return FACTS_PATH
