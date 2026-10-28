"""可复用任务技能库。

技能不是“自动执行脚本”，而是把一次成功任务的经验留下来：

- 大概步骤：从本次工具调用记录生成，只保留人话摘要，不保存工具返回正文；
- 关键截图 / 最终成果截图：复制到技能库自己的目录，原截图删了也不影响复用；
- 任务卡预设：记录允许动作、禁止动作、范围、预算，复用时可作为参考；
- **按子目标拆成多条技能**：“打开微信，再发消息”会存成两条，下次只说
  “打开微信”也能命中（schema v2）；
- **匹配条件与核对项**：`goal_match` 决定什么时候推荐它，`intents`/`verify`
  给出“建议怎么动”“做完怎么核对”。

设计取舍：
- 纯本地 JSON，不引入数据库；小企业单机场景够用；
- 匹配用字符 2-gram + 包含关系，不引向量库（无新依赖、结果可复现）；
- 技能只作为提示词参考，仍要经过当前任务卡、人工确认和 PIN；
- 不自动把技能里的步骤当成“已执行”，避免旧经验覆盖现场事实。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from agent.guard.task_card import split_subgoals
from agent.tools.safety import (
    describe_operation,
    operation_target,
    sanitize_tool_args,
)
from utils.logger_handler import logger
from utils.path_tool import get_abs_path

_SCREENSHOT_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
_MAX_SCREENSHOT_BYTES = 10 * 1024 * 1024
_MAX_SCREENSHOTS = 12
_MAX_STEPS = 80
_MAX_STEP_CHARS = 500
_MAX_SKILLS = 300
_MAX_GOAL_MATCH = 8
_MAX_INTENTS = 8
_MAX_VERIFY = 6
_MAX_MATCH_RESULTS = 3
_SCHEMA_VERSION = 2
# 匹配分低于这个值就不推荐（避免“常驻噪声”）；包含关系直接给满分。
_MATCH_MIN_SCORE = 0.34
_CARD_FIELDS = (
    "allowed_actions",
    "forbidden_actions",
    "input_scope",
    "output_location",
    "machine_checks",
    "max_steps",
    "max_minutes",
    "max_cost_yuan",
    "valid_minutes",
)
_SCREENSHOT_RE = re.compile(
    r"截图文件[：:]\s*(.+?\.(?:png|jpe?g|webp))",
    re.IGNORECASE,
)


def skills_dir() -> Path:
    """技能库根目录；测试可用 AGENT_SKILL_DIR 隔离。"""
    override = os.getenv("AGENT_SKILL_DIR")
    base = Path(override) if override else Path(get_abs_path("data/task_skills"))
    return base


def _skills_file() -> Path:
    return skills_dir() / "skills.json"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _atomic_write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with open(temp, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
    os.replace(temp, path)


def _normalise_skill(item: dict) -> dict:
    """把旧版/手工改过的技能补齐成 schema v2 结构（只补不删）。

    旧库只有 `name/description/steps/task_card`，这里补上：
    `goal_match`（默认用技能名）、`intents=[]`、`verify=[]`。
    """
    data = dict(item)
    name = str(data.get("name") or "").strip()
    data["name"] = name
    data["steps"] = _clean_steps(data.get("steps"))

    goal_match = [str(value).strip() for value in (data.get("goal_match") or []) if str(value).strip()]
    if not goal_match and name:
        goal_match = [name]
    data["goal_match"] = list(dict.fromkeys(goal_match))[:_MAX_GOAL_MATCH]

    intents: list[dict] = []
    for intent in data.get("intents") or []:
        if not isinstance(intent, dict) or not intent.get("kind"):
            continue
        intents.append(
            {
                "kind": str(intent["kind"])[:24],
                "target": str(intent.get("target") or "")[:120],
            }
        )
    data["intents"] = intents[:_MAX_INTENTS]

    verify = [str(value)[:200] for value in (data.get("verify") or []) if str(value).strip()]
    data["verify"] = list(dict.fromkeys(verify))[:_MAX_VERIFY]

    data.setdefault("key_screenshots", [])
    data.setdefault("final_screenshots", [])
    data.setdefault("use_count", 0)
    data["schema"] = _SCHEMA_VERSION
    return data


def _read_all() -> list[dict]:
    path = _skills_file()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        logger.warning("[skills]技能库 JSON 无法读取，按空库处理")
        return []
    if not isinstance(data, list):
        return []
    return [_normalise_skill(item) for item in data if isinstance(item, dict) and item.get("id")]


def _public(skill: dict) -> dict:
    """返回一份可供界面使用的副本，并补齐截图绝对路径。"""
    result = dict(skill)
    for key in ("key_screenshots", "final_screenshots"):
        resolved: list[str] = []
        for item in skill.get(key, []):
            path = resolve_screenshot(item)
            if path:
                resolved.append(str(path))
        result[key] = resolved
    return result


def list_skills() -> list[dict]:
    """按更新时间倒序列出技能。"""
    skills = [dict(item) for item in _read_all()]
    return sorted(
        skills,
        key=lambda item: str(item.get("updated_at") or ""),
        reverse=True,
    )


def get_skill(skill_id: str) -> dict | None:
    """读取一个技能；返回截图绝对路径。"""
    if not skill_id:
        return None
    for item in _read_all():
        if item.get("id") == skill_id:
            return _public(item)
    return None


def resolve_screenshot(value: str) -> Path | None:
    """把技能里保存的相对路径解析为绝对路径。"""
    if not value:
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = skills_dir() / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    return resolved if resolved.is_file() else None


def _clean_steps(steps: list[str] | str | None) -> list[str]:
    if isinstance(steps, str):
        raw_lines = steps.splitlines()
    else:
        raw_lines = list(steps or [])
    cleaned: list[str] = []
    for line in raw_lines:
        text = str(line).strip()
        if not text:
            continue
        if len(text) > _MAX_STEP_CHARS:
            text = text[:_MAX_STEP_CHARS] + "…"
        cleaned.append(text)
        if len(cleaned) >= _MAX_STEPS:
            break
    return cleaned


def _safe_target(name: str, args: dict) -> str:
    """生成不会把命令/密钥原文带进技能库的目标摘要。"""
    if name == "run_command":
        return "（命令内容已省略，复用前请按现场重新确认）"
    if name == "open_path":
        raw = str(args.get("path") or "")
        if raw.startswith(("http://", "https://")):
            return "（URL 已省略，复用前请按现场重新确认）"
    try:
        return str(operation_target(name, args) or "")
    except Exception:  # noqa: BLE001 - 技能记录不能因单个参数失败
        return ""


def steps_from_operations(operations: list[dict] | None) -> list[str]:
    """把工具调用记录转成“大概步骤”。

    只保存标题、目标路径/动作和状态，不保存工具返回正文；`computer_use`
    的文字输入和剪贴板写入会先脱敏。
    """
    steps: list[str] = []
    for index, operation in enumerate(operations or [], start=1):
        if not isinstance(operation, dict):
            continue
        name = str(operation.get("name") or "")
        raw_args = operation.get("args")
        args = dict(raw_args) if isinstance(raw_args, dict) else {}
        safe_args = sanitize_tool_args(name, args)
        try:
            title, _detail = describe_operation(name, safe_args)
        except Exception:  # noqa: BLE001
            title = name or "电脑操作"
        target = _safe_target(name, safe_args)
        status = str(operation.get("status") or "success")
        prefix = "" if status == "success" else "（未完成）"
        text = f"{index}. {title}{prefix}"
        if target:
            text += f"：{target}"
        steps.append(text)
        if len(steps) >= _MAX_STEPS:
            break
    return steps


def _normalise_text(text: object) -> str:
    """匹配用的归一：小写、去空白与常见标点。"""
    raw = str(text or "").lower()
    return "".join(ch for ch in raw if not ch.isspace() and ch not in "，。；;、,.:：!！?？()（）[]【】")


def _ngrams(text: str, size: int = 2) -> set[str]:
    if len(text) <= size:
        return {text} if text else set()
    return {text[index : index + size] for index in range(len(text) - size + 1)}


def _similarity(first: str, second: str) -> float:
    """字符 2-gram 的 Jaccard 相似度（不引向量库，结果可复现）。"""
    left, right = _ngrams(_normalise_text(first)), _ngrams(_normalise_text(second))
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _operation_text(operation: dict) -> str:
    """把一个工具调用压成可比较的文字（标题 + 目标）。"""
    name = str(operation.get("name") or "")
    raw = operation.get("args")
    args = dict(raw) if isinstance(raw, dict) else {}
    safe = sanitize_tool_args(name, args)
    try:
        title, detail = describe_operation(name, safe)
    except Exception:  # noqa: BLE001 - 技能记录不能因单个参数失败
        title, detail = name, ""
    target = _safe_target(name, safe)
    return " ".join(part for part in (title, detail, target) if part)


def skill_name_from_goal(goal: str, limit: int = 40) -> str:
    """从子目标/输入里生成一个短技能名。"""
    text = " ".join(str(goal or "").split()).strip(" 。；;，,、")
    return text[:limit] or "快速技能"


# computer_use 内部是按 action 区分的，单独一张表；观察类动作不入意图。
_CU_ACTION_INTENTS = {
    "app_open": "app_open",
    "app_open_start_menu": "app_open",
    "app_close": "close_app",
    "ui_click": "click",
    "click": "click",
    "double_click": "click",
    "right_click": "click",
    "click_text": "click_text",
    "ui_set_text": "set_text",
    "type_text": "type_text",
    "hotkey": "hotkey",
    "press_key": "key",
}
_TOOL_INTENTS = {
    "open_path": "open_path",
    "run_command": "command",
    "write_text_file": "write_file",
    "create_directory": "create_dir",
    "move_path": "move",
    "copy_path": "copy",
    "delete_path": "delete",
}


def intents_from_operations(operations: list[dict] | None) -> list[dict]:
    """把工具调用提炼成“建议动作”列表（只作提示，不自动执行）。"""
    intents: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for operation in operations or []:
        if not isinstance(operation, dict):
            continue
        name = str(operation.get("name") or "")
        raw = operation.get("args")
        args = dict(raw) if isinstance(raw, dict) else {}
        if name == "computer_use":
            kind = _CU_ACTION_INTENTS.get(str(args.get("action") or ""))
        else:
            kind = _TOOL_INTENTS.get(name)
        if not kind:
            continue
        safe = sanitize_tool_args(name, args)
        target = _safe_target(name, safe)
        key = (kind, target)
        if key in seen:
            continue
        seen.add(key)
        intents.append({"kind": kind, "target": target})
        if len(intents) >= _MAX_INTENTS:
            break
    return intents


def derive_verify(operations: list[dict] | None) -> list[str]:
    """从写类操作推导可机器核对的条件（沿用机器判据的 `exists|路径` 语法）。"""
    checks: list[str] = []
    for operation in operations or []:
        if not isinstance(operation, dict):
            continue
        if str(operation.get("status") or "success").lower() == "error":
            continue
        name = str(operation.get("name") or "")
        raw = operation.get("args")
        args = dict(raw) if isinstance(raw, dict) else {}
        if name in {"write_text_file", "create_directory"} and args.get("path"):
            checks.append(f"exists|{args['path']}")
        elif name in {"copy_path", "move_path"} and args.get("destination"):
            checks.append(f"exists|{args['destination']}")
    return list(dict.fromkeys(checks))[:_MAX_VERIFY]


def _best_match_score(goal_text: str, titles: list[str], description: str = "") -> float:
    """打分：标题（名字/匹配词）看包含关系，描述只作弱信号。

    只按“描述里出现过目标”给高分会造成假阳性：例如技能“从开始菜单打开软件”的描述
    里写了“打开微信、QQ……”，就会被“打开微信”误命中。所以：
    - 标题被目标包含 → 1.0；目标被标题包住 → 0.95；否则算相似度；
    - 描述只取相似度的 0.8 倍，且不参与包含判定。
    """
    best = 0.0
    for raw in titles:
        text = _normalise_text(raw)
        if len(text) < 2:
            continue
        if text in goal_text:
            return 1.0
        if len(goal_text) >= 2 and goal_text in text:
            best = max(best, 0.95)
            continue
        best = max(best, _similarity(goal_text, text))
    desc = _normalise_text(description)
    if desc:
        best = max(best, _similarity(goal_text, desc) * 0.8)
    return best


def match_skills(goal: str, limit: int = _MAX_MATCH_RESULTS) -> list[dict]:
    """按目标文本推荐技能（返回带 match_score 的技能副本）。"""
    goal_text = _normalise_text(goal)
    if not goal_text:
        return []
    scored: list[dict] = []
    for skill in _read_all():
        titles = [str(skill.get("name") or ""), *[str(item) for item in skill.get("goal_match") or []]]
        score = _best_match_score(goal_text, titles, str(skill.get("description") or ""))
        if score < _MATCH_MIN_SCORE:
            continue
        item = _public(skill)
        item["match_score"] = round(score, 3)
        scored.append(item)
    scored.sort(key=lambda item: (-float(item["match_score"]), -int(item.get("use_count") or 0)))
    return scored[: max(1, limit)]


def reference_lines(skill: dict, limit: int = 12) -> list[str]:
    """把技能转成提示词里的参考行（步骤 + 建议动作 + 核对项）。"""
    lines = [str(item) for item in skill.get("steps") or [] if str(item).strip()]
    intents = [
        f"{item.get('kind')}：{item.get('target')}"
        for item in skill.get("intents") or []
        if isinstance(item, dict) and item.get("kind")
    ]
    if intents:
        lines.append("建议动作（仍需按现场核对）：" + "；".join(intents))
    verify = [str(item) for item in skill.get("verify") or [] if str(item).strip()]
    if verify:
        lines.append("完成后核对：" + "；".join(verify))
    return lines[:limit]


def split_skill_candidates(goal: str, operations: list[dict] | None) -> list[dict]:
    """把一次任务按子目标切成多个候选技能（默认值，界面可改）。

    解决的问题：存一条“打开微信→发消息”的长技能，下次只说“打开微信”就
    匹配不上。拆分后每条技能都有自己能命中的名字与匹配词。

    归属规则（确定性、可解释）：先按位置比例给每步一个默认段，再用 2-gram
    相似度在同一段或更晚的段里修正；只能向前，避免步骤回头串到前一个子目标。
    """
    ops = [op for op in (operations or []) if isinstance(op, dict)]
    subgoals = split_subgoals(goal)
    if len(subgoals) < 2 or not ops:
        return [
            {
                "name": skill_name_from_goal(goal),
                "goal_match": [skill_name_from_goal(goal)],
                "steps": steps_from_operations(ops),
                "intents": intents_from_operations(ops),
                "verify": derive_verify(ops),
            }
        ]

    total = len(subgoals)
    buckets: list[list[dict]] = [[] for _ in range(total)]
    stage = 0
    for index, operation in enumerate(ops):
        ratio_stage = min(total - 1, index * total // max(1, len(ops)))
        text = _operation_text(operation)
        best_index, best_score = stage, 0.0
        for candidate in range(stage, total):
            score = _similarity(text, subgoals[candidate])
            if score > best_score:
                best_index, best_score = candidate, score
        stage = best_index if best_score >= 0.3 else max(stage, ratio_stage)
        buckets[stage].append(operation)

    candidates: list[dict] = []
    for index, subgoal in enumerate(subgoals):
        bucket = buckets[index]
        if not bucket:
            continue
        name = skill_name_from_goal(subgoal)
        candidates.append(
            {
                "name": name,
                # 名字就是从子目标截出来的，不再重复放进匹配词
                "goal_match": list(dict.fromkeys([name, subgoal])),
                "steps": steps_from_operations(bucket),
                "intents": intents_from_operations(bucket),
                "verify": derive_verify(bucket),
            }
        )
    if not candidates:
        return [
            {
                "name": skill_name_from_goal(goal),
                "goal_match": [skill_name_from_goal(goal)],
                "steps": steps_from_operations(ops),
                "intents": intents_from_operations(ops),
                "verify": derive_verify(ops),
            }
        ]
    return candidates


def _copy_screenshot(source: str, skill_id: str, index: int) -> str | None:
    """复制一张截图到技能库；返回相对路径。非图片/超大/不存在时跳过。

    如果源文件已经在这个技能自己的截图目录里（编辑已保存技能时会把相对
    路径回传成绝对路径），直接复用原相对路径，不再重新复制；否则会每次
    编辑都叠一份 `00_01_xxx.png` 且旧件不清理。
    """
    try:
        path = Path(source).resolve()
    except OSError:
        return None
    if not path.is_file() or path.suffix.lower() not in _SCREENSHOT_SUFFIXES:
        return None
    base = skills_dir().resolve()
    own_dir = base / "screenshots" / skill_id
    try:
        if path.is_relative_to(own_dir):
            return path.relative_to(base).as_posix()
    except (OSError, ValueError):
        pass
    try:
        if path.stat().st_size > _MAX_SCREENSHOT_BYTES:
            logger.warning(f"[skills]截图超过 10MB，已跳过：{path}")
            return None
    except OSError:
        return None

    target_dir = own_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{index:02d}_{path.name}"
    try:
        shutil.copy2(path, target)
    except OSError as exc:
        logger.warning(f"[skills]复制截图失败（已跳过）：{exc}")
        return None
    return target.relative_to(skills_dir()).as_posix()


def _copy_screenshots(paths: list[str], skill_id: str, offset: int = 0) -> list[str]:
    result: list[str] = []
    for index, source in enumerate(paths[:_MAX_SCREENSHOTS], start=offset):
        copied = _copy_screenshot(source, skill_id, index)
        if copied:
            result.append(copied)
    return result


def _find_by_name(skills: list[dict], name: str) -> dict | None:
    """按技能名查找已有技能；同名只比首尾去空白后的标题。"""
    title = str(name or "").strip()
    if not title:
        return None
    for item in skills:
        if str(item.get("name") or "").strip() == title:
            return item
    return None


def save_skill(
    *,
    name: str,
    description: str = "",
    steps: list[str] | str | None = None,
    key_screenshots: list[str] | None = None,
    final_screenshots: list[str] | None = None,
    task_card: dict | None = None,
    skill_id: str = "",
    goal_match: list[str] | None = None,
    intents: list[dict] | None = None,
    verify: list[str] | None = None,
) -> dict:
    """新增或更新一个技能，返回保存后的公开结构。

    未传 `skill_id` 时按技能名对齐：同名视为更新而不是另建一份，
    避免界面上重复点“保存”堆出一堆同名技能、看起来“没反应”。

    `goal_match` / `intents` / `verify` 不传时保留旧值（编辑步骤不会把
    匹配词和核对项洗掉）；新建时由 `_normalise_skill` 用技能名补默认值。
    """
    title = str(name or "").strip()
    if not title:
        raise ValueError("技能名不能为空")
    if len(title) > 80:
        title = title[:80]

    skills = _read_all()
    existing = next((item for item in skills if item.get("id") == skill_id), None)
    if existing is None and not skill_id:
        # 技能库按“名称唯一”对普通用户更符合直觉：保存同名 = 更新这个技能。
        existing = _find_by_name(skills, title)
    if existing is None:
        if len(skills) >= _MAX_SKILLS:
            raise ValueError(f"技能库最多保留 {_MAX_SKILLS} 个技能")
        existing = {
            "id": uuid.uuid4().hex[:12],
            "created_at": _now(),
            "use_count": 0,
        }
    else:
        existing = dict(existing)

    sid = str(existing["id"])
    card = task_card if isinstance(task_card, dict) else {}
    preset = {key: card[key] for key in _CARD_FIELDS if key in card}
    existing.update(
        {
            "name": title,
            "description": str(description or "").strip()[:500],
            "steps": _clean_steps(steps),
            "task_card": preset,
            "updated_at": _now(),
        }
    )
    if goal_match is not None:
        existing["goal_match"] = goal_match
    if intents is not None:
        existing["intents"] = intents
    if verify is not None:
        existing["verify"] = verify
    existing = _normalise_skill(existing)

    new_keys = _copy_screenshots(list(key_screenshots or []), sid, 0)
    new_finals = _copy_screenshots(list(final_screenshots or []), sid, 20)
    if new_keys:
        existing["key_screenshots"] = new_keys
    if new_finals:
        existing["final_screenshots"] = new_finals
    existing.setdefault("key_screenshots", [])
    existing.setdefault("final_screenshots", [])

    replaced = False
    for index, item in enumerate(skills):
        if item.get("id") == sid:
            skills[index] = existing
            replaced = True
            break
    if not replaced:
        skills.append(existing)
    _atomic_write(_skills_file(), skills)
    return get_skill(sid) or existing


def delete_skill(skill_id: str) -> bool:
    """删除技能及其截图目录；只操作技能库内部路径。"""
    if not skill_id:
        return False
    skills = _read_all()
    kept = [item for item in skills if item.get("id") != skill_id]
    if len(kept) == len(skills):
        return False
    _atomic_write(_skills_file(), kept)
    base = skills_dir().resolve()
    target = (base / "screenshots" / skill_id).resolve()
    if target == base or base not in target.parents:
        return True
    shutil.rmtree(target, ignore_errors=True)
    return True


def mark_skill_used(skill_id: str) -> None:
    """记录一次复用（只用于展示，不参与安全判断）。"""
    skills = _read_all()
    changed = False
    for item in skills:
        if item.get("id") == skill_id:
            item["use_count"] = int(item.get("use_count") or 0) + 1
            item["last_used_at"] = _now()
            changed = True
            break
    if changed:
        _atomic_write(_skills_file(), skills)


def screenshot_candidates(operations: list[dict] | None = None, limit: int = 12) -> list[str]:
    """列出本次任务截图；没有时回退到最近的 OmniPilot 截图。"""
    found: list[str] = []
    for operation in operations or []:
        if not isinstance(operation, dict):
            continue
        text = str(operation.get("result") or "")
        for match in _SCREENSHOT_RE.findall(text):
            candidate = match.strip().strip('"').strip("'")
            path = Path(candidate)
            if path.is_file() and str(path) not in found:
                found.append(str(path))
            if len(found) >= limit:
                return found

    if found:
        return found

    directory = Path(get_abs_path("logs/omnipilot/screenshots"))
    if not directory.is_dir():
        return []
    try:
        files = [
            path
            for path in directory.iterdir()
            if path.is_file() and path.suffix.lower() in _SCREENSHOT_SUFFIXES
        ]
    except OSError:
        return []
    files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    return [str(path) for path in files[:limit]]


def skill_to_text(skill: dict) -> str:
    """把技能转成给模型/界面看的可读参考。"""
    lines = reference_lines(skill)
    steps = "\n".join(f"- {item}" for item in lines)
    description = str(skill.get("description") or "").strip()
    return (
        f"技能：{skill.get('name', '')}\n"
        f"适用场景：{description or '未填写'}\n"
        f"参考步骤：\n{steps or '- （未填写）'}"
    )
