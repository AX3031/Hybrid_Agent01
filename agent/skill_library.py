"""可复用任务技能库。

技能不是“自动执行脚本”，而是把一次成功任务的经验留下来：

- 大概步骤：从本次工具调用记录生成，只保留人话摘要，不保存工具返回正文；
- 关键截图 / 最终成果截图：复制到技能库自己的目录，原截图删了也不影响复用；
- 任务卡预设：记录允许动作、禁止动作、范围、预算，复用时可作为参考。

设计取舍：
- 纯本地 JSON，不引入数据库；小企业单机场景够用；
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
    return [item for item in data if isinstance(item, dict) and item.get("id")]


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


def _copy_screenshot(source: str, skill_id: str, index: int) -> str | None:
    """复制一张截图到技能库；返回相对路径。非图片/超大/不存在时跳过。"""
    try:
        path = Path(source).resolve()
    except OSError:
        return None
    if not path.is_file() or path.suffix.lower() not in _SCREENSHOT_SUFFIXES:
        return None
    try:
        if path.stat().st_size > _MAX_SCREENSHOT_BYTES:
            logger.warning(f"[skills]截图超过 10MB，已跳过：{path}")
            return None
    except OSError:
        return None

    target_dir = skills_dir() / "screenshots" / skill_id
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


def save_skill(
    *,
    name: str,
    description: str = "",
    steps: list[str] | str | None = None,
    key_screenshots: list[str] | None = None,
    final_screenshots: list[str] | None = None,
    task_card: dict | None = None,
    skill_id: str = "",
) -> dict:
    """新增或更新一个技能，返回保存后的公开结构。"""
    title = str(name or "").strip()
    if not title:
        raise ValueError("技能名不能为空")
    if len(title) > 80:
        title = title[:80]

    skills = _read_all()
    existing = next((item for item in skills if item.get("id") == skill_id), None)
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
    steps = "\n".join(f"- {item}" for item in skill.get("steps", []))
    description = str(skill.get("description") or "").strip()
    return (
        f"技能：{skill.get('name', '')}\n"
        f"适用场景：{description or '未填写'}\n"
        f"参考步骤：\n{steps or '- （未填写）'}"
    )
