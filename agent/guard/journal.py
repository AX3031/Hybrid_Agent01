"""批次日志与整批回滚（P0-3）。

为什么需要“批次”：

- 现有的 `.agent_trash` / `.agent_backup` 只能撤销**单个动作**；
- 一次真实任务往往包含几十个写入、移动、复制、删除，出问题时用户需要
  的是“把这一个任务的全部改动一起撤掉”，而不是逐个文件恢复；
- 审计也必须回答“这次任务改了哪些东西”，批次号就是任务和改动之间的
  那根线。

实现取舍（小企业版）：

- 每个批次一个 JSONL 文件，追加写、不建数据库，出问题能直接打开看；
- 只记录撤销需要的最小信息（路径、备份位置、原大小），不复制正文；
- 回滚逐条执行并汇总“已恢复 / 失败 / 不可逆”，绝不静默跳过；
- 回滚本身也写一条记录，保证日志是追加历史，而不是被改写。
"""

import hashlib
import json
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from agent.guard.config import state_dir
from utils import audit_logger
from utils.logger_handler import logger

_BATCH_DIR_NAME = "batches"
_LOCK = threading.RLock()

# 本模块只做“记录与撤销”，不引用 agent.tools.safety；撤销需要回收区时
# 在函数内部延迟导入，避免 safety -> guard.approval -> guard 的循环依赖。


def _batches_dir() -> Path:
    path = state_dir() / _BATCH_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _batch_path(batch_id: str) -> Path:
    safe = "".join(ch for ch in str(batch_id) if ch.isalnum() or ch in "-_")
    return _batches_dir() / f"{safe or 'unknown'}.jsonl"


def _current_batch_id() -> str:
    """取当前任务的批次号；没有任务时退回按天的手工批次。"""
    try:
        from agent.guard import state as guard_state

        batch_id = guard_state.current_batch_id()
        if batch_id:
            return batch_id
    except Exception:  # noqa: BLE001 - 记录失败不应影响工具本身
        pass
    return f"B{datetime.now():%Y%m%d}-manual"


def new_batch_id() -> str:
    """生成任务批次号：B + 日期 + 时间 + 4 位随机串。"""
    suffix = hashlib.sha1(f"{time.time_ns()}-{os.getpid()}".encode()).hexdigest()[:4]
    return f"B{datetime.now():%Y%m%d-%H%M%S}-{suffix}"


def _append(entry: dict, batch_id: str | None = None) -> None:
    """追加一条批次记录；失败只记日志，不影响主流程。"""
    identifier = batch_id or _current_batch_id()
    record = {
        "batch_id": identifier,
        "ts": datetime.now().isoformat(timespec="seconds"),
        **entry,
    }
    try:
        with _LOCK:
            path = _batch_path(identifier)
            lines = 0
            if path.exists():
                try:
                    with open(path, "r", encoding="utf-8") as stream:
                        lines = sum(1 for _ in stream)
                except OSError:
                    lines = 0
            record["seq"] = lines + 1
            with open(path, "a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning(f"[batch]写入批次记录失败（已忽略）：{exc}")


def record_task_start(card: dict | None, batch_id: str, thread_id: str = "") -> None:
    """任务开始时写一条快照，回放时能看到这张卡。"""
    _append(
        {
            "op": "task_start",
            "thread_id": thread_id,
            "card": card or {},
        },
        batch_id,
    )


def record_write(path: Path, mode: str, existed: bool, size_before: int, backup: Path | None) -> None:
    _append(
        {
            "op": "write",
            "path": str(path),
            "mode": mode,
            "existed": bool(existed),
            "size_before": int(size_before),
            "backup": str(backup) if backup else "",
        }
    )


def record_mkdir(path: Path, existed: bool) -> None:
    _append({"op": "mkdir", "path": str(path), "existed": bool(existed)})


def record_move(src: Path, dst: Path) -> None:
    _append({"op": "move", "src": str(src), "dst": str(dst)})


def record_copy(src: Path, dst: Path) -> None:
    _append({"op": "copy", "src": str(src), "dst": str(dst)})


def record_rmdir(path: Path) -> None:
    _append({"op": "rmdir", "path": str(path)})


def record_trash(src: Path, trash: Path) -> None:
    _append({"op": "trash", "src": str(src), "trash": str(trash)})


def record_permanent_delete(path: Path) -> None:
    _append({"op": "delete_permanent", "path": str(path)})


def load_batch(batch_id: str) -> list[dict]:
    """读取一个批次的全部记录；文件损坏时尽量返回能解析的行。"""
    path = _batch_path(batch_id)
    if not path.exists():
        return []
    entries: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return entries


def batch_summary(batch_id: str) -> dict:
    """给界面用的批次摘要：任务名、操作数、是否已回滚、是否含不可逆操作。"""
    entries = load_batch(batch_id)
    card = {}
    ops: list[dict] = []
    rolled_back = False
    for entry in entries:
        op = entry.get("op")
        if op == "task_start":
            card = entry.get("card") or {}
        elif op == "rollback":
            rolled_back = True
        elif op:
            ops.append(entry)

    irreversible = [item for item in ops if item.get("op") == "delete_permanent"]
    first_ts = entries[0].get("ts", "") if entries else ""
    return {
        "batch_id": batch_id,
        "task_name": card.get("name") or "（未使用任务卡）",
        "goal": card.get("goal") or "",
        "created_at": first_ts,
        "ops": len(ops),
        "irreversible": len(irreversible),
        "rolled_back": rolled_back,
        "entries": entries,
    }


def list_batches(limit: int = 20) -> list[dict]:
    """按修改时间倒序列出最近的批次。"""
    directory = _batches_dir()

    def _mtime(item: Path) -> float:
        try:
            return item.stat().st_mtime
        except OSError:
            return 0.0

    files = sorted(directory.glob("*.jsonl"), key=_mtime, reverse=True)
    summaries: list[dict] = []
    for file in files[: max(1, min(limit, 100))]:
        summaries.append(batch_summary(file.stem))
    return summaries


# ---------------------------------------------------------------------------
# 回滚
# ---------------------------------------------------------------------------


@dataclass
class RollbackReport:
    """一次整批回滚的结论。"""

    batch_id: str
    restored: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    irreversible: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed and not self.irreversible

    def to_text(self) -> str:
        lines = [f"批次 {self.batch_id} 回滚结果："]
        lines.append(f"- 已恢复：{len(self.restored)} 项")
        if self.restored:
            lines.extend(f"  · {item}" for item in self.restored[:20])
        if self.irreversible:
            lines.append(f"- 不可逆（无法恢复）：{len(self.irreversible)} 项")
            lines.extend(f"  · {item}" for item in self.irreversible[:20])
        if self.failed:
            lines.append(f"- 失败：{len(self.failed)} 项")
            lines.extend(f"  · {item}" for item in self.failed[:20])
        if self.skipped:
            lines.append(f"- 跳过：{len(self.skipped)} 项")
        if not self.ok:
            lines.append("提示：失败或不可逆的项目需要人工处理，不要当成已经恢复。")
        return "\n".join(lines)


def _displace(path: Path, batch_id: str) -> Path:
    """回滚前把挡路的现有文件移走，优先用工作区回收区。"""
    try:
        from agent.tools.safety import move_to_trash

        moved, error = move_to_trash(path)
        if not error and moved:
            return moved
    except Exception:  # noqa: BLE001 - 回收区不可用时退回状态目录
        pass

    trash = state_dir() / "rollback_trash" / batch_id
    trash.mkdir(parents=True, exist_ok=True)
    dest = trash / path.name
    counter = 1
    while dest.exists():
        dest = trash / f"{counter}_{path.name}"
        counter += 1
    shutil.move(str(path), str(dest))
    return dest


def _undo_write(entry: dict, report: RollbackReport, batch_id: str) -> None:
    path = Path(str(entry.get("path", "")))
    mode = str(entry.get("mode") or "write")
    existed = bool(entry.get("existed"))

    if mode == "append" and existed:
        size_before = int(entry.get("size_before") or 0)
        if not path.exists():
            report.failed.append(f"追加回滚失败，文件已不存在：{path}")
            return
        try:
            with open(path, "r+b") as stream:
                stream.truncate(size_before)
            report.restored.append(f"已截断回追加前：{path}")
        except OSError as exc:
            report.failed.append(f"截断失败：{path}（{exc}）")
        return

    if not existed:
        if path.exists():
            dest = _displace(path, batch_id)
            report.restored.append(f"已移除新文件：{path}（移到 {dest}）")
        else:
            report.skipped.append(f"新文件已不存在：{path}")
        return

    backup = Path(str(entry.get("backup") or ""))
    if not entry.get("backup") or not backup.exists():
        report.failed.append(f"缺少覆盖前备份，无法恢复：{path}")
        return
    try:
        if path.exists():
            _displace(path, batch_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup, path)
        report.restored.append(f"已恢复原文件：{path}")
    except OSError as exc:
        report.failed.append(f"恢复失败：{path}（{exc}）")


def _undo_mkdir(entry: dict, report: RollbackReport) -> None:
    path = Path(str(entry.get("path", "")))
    if entry.get("existed"):
        report.skipped.append(f"目录原本就存在：{path}")
        return
    if not path.exists():
        report.skipped.append(f"目录已不存在：{path}")
        return
    try:
        path.rmdir()
        report.restored.append(f"已移除新建目录：{path}")
    except OSError:
        report.failed.append(f"目录非空，未删除：{path}")


def _undo_move(entry: dict, report: RollbackReport) -> None:
    src = Path(str(entry.get("src", "")))
    dst = Path(str(entry.get("dst", "")))
    if src.exists():
        report.failed.append(f"原位置已被占用，无法移回：{src}")
        return
    if not dst.exists():
        report.failed.append(f"移动目标不存在，无法移回：{dst}")
        return
    try:
        src.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(dst), str(src))
        report.restored.append(f"已移回：{dst} -> {src}")
    except OSError as exc:
        report.failed.append(f"移回失败：{dst} -> {src}（{exc}）")


def _undo_copy(entry: dict, report: RollbackReport, batch_id: str) -> None:
    dst = Path(str(entry.get("dst", "")))
    if not dst.exists():
        report.skipped.append(f"复制目标已不存在：{dst}")
        return
    try:
        dest = _displace(dst, batch_id)
        report.restored.append(f"已移除副本：{dst}（移到 {dest}）")
    except OSError as exc:
        report.failed.append(f"移除副本失败：{dst}（{exc}）")


def _undo_rmdir(entry: dict, report: RollbackReport) -> None:
    path = Path(str(entry.get("path", "")))
    if path.exists():
        report.skipped.append(f"目录已存在：{path}")
        return
    try:
        path.mkdir(parents=True, exist_ok=True)
        report.restored.append(f"已重建空目录：{path}")
    except OSError as exc:
        report.failed.append(f"重建目录失败：{path}（{exc}）")


def _undo_trash(entry: dict, report: RollbackReport) -> None:
    src = Path(str(entry.get("src", "")))
    trash = Path(str(entry.get("trash", "")))
    if src.exists():
        report.failed.append(f"原位置已被占用，无法从回收区还原：{src}")
        return
    if not trash.exists():
        report.failed.append(f"回收区文件不存在：{trash}")
        return
    try:
        src.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(trash), str(src))
        report.restored.append(f"已从回收区还原：{trash} -> {src}")
    except OSError as exc:
        report.failed.append(f"从回收区还原失败：{trash} -> {src}（{exc}）")


def rollback_batch(batch_id: str) -> RollbackReport:
    """按批次号逆序撤销整个任务；返回逐项结果。"""
    report = RollbackReport(batch_id=batch_id)
    entries = [entry for entry in load_batch(batch_id) if entry.get("op") not in {"task_start", "rollback"}]
    if not entries:
        report.skipped.append("该批次没有可撤销的操作")
        return report

    for entry in reversed(entries):
        op = entry.get("op")
        try:
            if op == "write":
                _undo_write(entry, report, batch_id)
            elif op == "mkdir":
                _undo_mkdir(entry, report)
            elif op == "move":
                _undo_move(entry, report)
            elif op == "copy":
                _undo_copy(entry, report, batch_id)
            elif op == "rmdir":
                _undo_rmdir(entry, report)
            elif op == "trash":
                _undo_trash(entry, report)
            elif op == "delete_permanent":
                report.irreversible.append(f"永久删除无法恢复：{entry.get('path', '')}")
            else:
                report.skipped.append(f"未知操作：{op}")
        except Exception as exc:  # noqa: BLE001 - 单项失败不能中断整批回滚
            report.failed.append(f"{op} 撤销异常：{exc}")

    _append(
        {
            "op": "rollback",
            "restored": len(report.restored),
            "failed": len(report.failed),
            "irreversible": len(report.irreversible),
            "ok": report.ok,
        },
        batch_id,
    )
    audit_logger.log_guard_event(
        "rollback",
        batch_id=batch_id,
        restored=len(report.restored),
        failed=len(report.failed),
        irreversible=len(report.irreversible),
        ok=report.ok,
    )
    logger.info(
        f"[batch]{batch_id} 回滚完成：恢复 {len(report.restored)}、"
        f"失败 {len(report.failed)}、不可逆 {len(report.irreversible)}"
    )
    return report
