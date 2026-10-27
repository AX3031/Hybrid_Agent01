"""单任务全局锁（P0-8）。

一台桌面只有一个焦点、一个剪贴板、一个前台窗口；两个任务同时跑会互相
抢资源，产生“看起来随机”的失败。小企业版的做法不是排队系统，而是一个
**互斥文件锁**：同一时刻只允许一个任务操作本机。

实现：

- `STATE_DIR/task.lock` 用 `O_CREAT|O_EXCL` 原子创建；
- 文件里写 PID、线程/会话 ID、获取时间；
- 进程崩溃留下的锁：PID 已不存在，或锁文件超过 6 小时，自动视为过期；
- 释放时校验持有者 token，避免误删别人的锁。
"""

import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import psutil

from agent.guard.config import state_dir
from utils.logger_handler import logger

_LOCK_NAME = "task.lock"
_STALE_SECONDS = 6 * 60 * 60


class TaskBusyError(RuntimeError):
    """已经有一个任务在操作本机。"""


def _lock_path() -> Path:
    return state_dir() / _LOCK_NAME


def _read_lock(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, json.JSONDecodeError):
        return {}


def _is_stale(path: Path, info: dict) -> bool:
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return True
    if age > _STALE_SECONDS:
        return True
    pid = info.get("pid")
    if not isinstance(pid, int):
        return True
    try:
        return not psutil.pid_exists(pid)
    except Exception:  # noqa: BLE001 - 无法判断时按“不 stale”处理，宁可报忙
        return False


@contextmanager
def task_lock(thread_id: str = ""):
    """获取单任务锁；拿不到就抛 `TaskBusyError`，由界面提示用户。"""
    path = _lock_path()
    token = uuid.uuid4().hex
    info = {
        "pid": os.getpid(),
        "token": token,
        "thread_id": thread_id,
        "acquired_at": time.time(),
    }

    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(info, stream, ensure_ascii=False)
            break
        except FileExistsError:
            existing = _read_lock(path)
            if _is_stale(path, existing):
                logger.warning(f"[guard]发现过期任务锁，已清理：{existing}")
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                continue
            owner = existing.get("thread_id") or "未知会话"
            started = existing.get("acquired_at")
            started_text = (
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started))
                if isinstance(started, (int, float))
                else "未知时间"
            )
            raise TaskBusyError(
                f"另一个任务正在操作本机（会话 {owner}，开始于 {started_text}）。"
                "请等它结束，或在确认它已经卡死后删除 .agent_state/task.lock。"
            )

    try:
        yield
    finally:
        current = _read_lock(path)
        if current.get("token") == token:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning(f"[guard]释放任务锁失败：{exc}")
