"""任务运行时状态：预算计数、急停、会话快照、审批上下文。

设计原则：

- **单一当前任务**：小企业版就是“一台电脑一个用户一个任务”，所以用
  进程内单例 + 文件快照，不引入数据库；
- **可恢复**：等待人工确认会把任务挂起，恢复时要接着用同一个批次号和
  同一份预算，不能重新从零开始；
- **停止不依赖模型**：急停既看进程内标志，也看 `STATE_DIR/STOP` 文件，
  外部脚本或界面按钮都能叫停；
- **失败安全**：状态文件写失败只记日志，不阻断主流程，但停止标志会
  在下一次检查时生效。
"""

import hashlib
import json
import os
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from agent.guard import journal
from agent.guard.config import model_prices, state_dir, task_defaults
from agent.guard.task_card import TaskCard
from utils import audit_logger
from utils.logger_handler import logger

_LOCK = threading.RLock()
_CURRENT: "TaskRuntime | None" = None
_APPROVAL: dict = {}

# 审批上下文的落盘副本。
#
# 为什么不只放内存：Streamlit 在本地模块文件变化时会卸载并重新导入模块，
# 但 `st.session_state` 里缓存的旧 Agent 不会重建——旧中间件仍持有旧
# `agent.guard.state` 模块，而界面侧写的是新模块的 `_APPROVAL`。
# 结果就是“PIN 正确、凭证也生成了，执行端却一直说没有凭证”（真实事故：
# 2026-10-28 打开微信连续 5 次被拒）。文件是唯一能跨模块实例共享的介质。
#
# 安全边界：文件里只有 HMAC 凭证摘要，没有 PIN；执行端仍会校验签名、
# 有效期和任务/批次/动作绑定，伪造文件过不了验签。
_APPROVAL_FILE = "approval_context.json"
_APPROVAL_FILE_TTL_SECONDS = 15 * 60


class TaskStopped(RuntimeError):
    """急停尚未解除时拒绝启动新任务。"""


@dataclass
class TaskRuntime:
    """一个任务的运行时账本。"""

    task_id: str
    thread_id: str
    batch_id: str
    card: dict | None
    started_at: float
    max_steps: int
    max_minutes: int
    max_cost_yuan: float
    valid_minutes: int = 120
    steps: int = 0
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_yuan: float = 0.0
    stop_reason: str = ""
    status: str = "running"
    tool_errors: int = 0
    verification_status: str = ""
    saved_at: float = field(default_factory=time.time)

    def to_snapshot(self) -> dict:
        return asdict(self)

    @classmethod
    def from_snapshot(cls, data: dict) -> "TaskRuntime":
        return cls(
            task_id=str(data.get("task_id") or uuid.uuid4().hex[:12]),
            thread_id=str(data.get("thread_id") or ""),
            batch_id=str(data.get("batch_id") or journal.new_batch_id()),
            card=data.get("card"),
            started_at=float(data.get("started_at") or time.time()),
            max_steps=int(data.get("max_steps") or task_defaults()["max_steps"]),
            max_minutes=int(data.get("max_minutes") or task_defaults()["max_minutes"]),
            max_cost_yuan=float(data.get("max_cost_yuan") or task_defaults()["max_cost_yuan"]),
            valid_minutes=int(data.get("valid_minutes") or 120),
            steps=int(data.get("steps") or 0),
            model_calls=int(data.get("model_calls") or 0),
            input_tokens=int(data.get("input_tokens") or 0),
            output_tokens=int(data.get("output_tokens") or 0),
            cost_yuan=float(data.get("cost_yuan") or 0.0),
            stop_reason=str(data.get("stop_reason") or ""),
            status=str(data.get("status") or "running"),
            tool_errors=int(data.get("tool_errors") or 0),
            verification_status=str(data.get("verification_status") or ""),
            saved_at=float(data.get("saved_at") or time.time()),
        )


def _snapshot_path(thread_id: str) -> Path:
    directory = state_dir() / "sessions"
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1((thread_id or "default").encode("utf-8")).hexdigest()[:16]
    return directory / f"{digest}.json"


def _persist(task: TaskRuntime) -> None:
    """写会话快照；失败只告警，不影响当前任务继续。"""
    task.saved_at = time.time()
    try:
        path = _snapshot_path(task.thread_id)
        temp = path.with_suffix(".json.tmp")
        with open(temp, "w", encoding="utf-8") as stream:
            json.dump(task.to_snapshot(), stream, ensure_ascii=False, indent=2)
        os.replace(temp, path)
    except OSError as exc:
        logger.warning(f"[guard]保存任务快照失败（已忽略）：{exc}")


def _load_snapshot(thread_id: str) -> TaskRuntime | None:
    path = _snapshot_path(thread_id)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as stream:
            return TaskRuntime.from_snapshot(json.load(stream))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _stop_file() -> Path:
    return state_dir() / "STOP"


def _stop_file_reason() -> str:
    path = _stop_file()
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return "急停已触发（无法读取原因）"
    first = text.splitlines()[0].strip() if text else ""
    return first or "急停已触发"


def _card_to_dict(card: TaskCard | dict | None) -> dict | None:
    """把任务卡统一成 dict。

    不能只靠 `isinstance(card, TaskCard)`：Streamlit 热重载会重新执行
    `task_card` 模块生成新的类对象，而 `state` 还持有旧类引用，于是“同一个
    TaskCard”判定为 False，走到 `dict(card)` 就报 `'TaskCard' object is not
    iterable`（真实事故：确认后继续执行时直接失败）。这里按能力判断：
    有 `to_dict()` 就当任务卡；映射类型就浅拷贝；其它类型给出可读报错，
    绝不能静默变成 `{}`（那会让任务卡失效、守卫范围变宽）。
    """
    if card is None:
        return None
    to_dict = getattr(card, "to_dict", None)
    if callable(to_dict):
        data = to_dict()
        return dict(data) if isinstance(data, Mapping) else data
    if isinstance(card, Mapping):
        return dict(card)
    raise TypeError(
        f"任务卡类型不支持：{type(card).__module__}.{type(card).__name__}；"
        "请传 TaskCard 或 dict。"
    )


def start_task(
    thread_id: str,
    card: TaskCard | dict | None = None,
    *,
    batch_id: str | None = None,
    resume: bool = False,
) -> TaskRuntime:
    """开始或恢复一个任务。

    - 新任务：生成新的批次号，写入任务卡快照；
    - 恢复（等待审批后继续）：沿用原批次号，并把“等人”的时间从时长
      预算里扣除，避免因为人离开一会儿就把任务判定超时。
    """
    global _CURRENT

    card_dict = _card_to_dict(card)
    if not card_dict and not resume:
        raise ValueError(
            "每个新任务都必须有任务卡；界面会按输入自动生成快速卡，"
            "直接调用 API 时请显式传入 TaskCard（可用 TaskCard.from_prompt(...)）。"
        )
    if card_dict:
        errors = TaskCard.from_dict(card_dict).validate()  # type: ignore[union-attr]
        if errors:
            raise ValueError("任务卡不合法：" + "；".join(errors))

    with _LOCK:
        stop = _stop_file_reason()
        if stop:
            raise TaskStopped(stop)

        if resume:
            task = _load_snapshot(thread_id)
            if task is None:
                if not card_dict:
                    raise ValueError("恢复任务失败：没有找到任务卡快照，请重新发起任务。")
                task = _new_task(thread_id, card_dict, batch_id)
            else:
                idle = max(0.0, time.time() - task.saved_at)
                task.started_at += idle
                if card_dict:
                    task.card = card_dict
                if batch_id:
                    task.batch_id = batch_id
                task.status = "running"
                if task.card:
                    budgets = TaskCard.from_dict(task.card)
                    if budgets:
                        task.max_steps = budgets.max_steps
                        task.max_minutes = budgets.max_minutes
                        task.max_cost_yuan = budgets.max_cost_yuan
                        task.valid_minutes = budgets.valid_minutes
                # 恢复时清空上一次的停止原因，但 STOP 文件仍然优先
                task.stop_reason = ""
        else:
            task = _new_task(thread_id, card_dict, batch_id)

        _CURRENT = task
        _persist(task)
        return task


def _new_task(thread_id: str, card_dict: dict | None, batch_id: str | None) -> TaskRuntime:
    budgets = TaskCard.from_dict(card_dict) if card_dict else None
    defaults = task_defaults()
    task = TaskRuntime(
        task_id=uuid.uuid4().hex[:12],
        thread_id=thread_id,
        batch_id=batch_id or journal.new_batch_id(),
        card=card_dict,
        started_at=time.time(),
        max_steps=budgets.max_steps if budgets else defaults["max_steps"],
        max_minutes=budgets.max_minutes if budgets else defaults["max_minutes"],
        max_cost_yuan=budgets.max_cost_yuan if budgets else defaults["max_cost_yuan"],
        valid_minutes=budgets.valid_minutes if budgets else 120,
    )
    journal.record_task_start(card_dict, task.batch_id, thread_id)
    audit_logger.log_task_start(task.task_id, task.batch_id, thread_id, card_dict)
    return task


def end_task(status: str = "finished") -> None:
    """结束当前任务并保存最终状态。"""
    global _CURRENT
    with _LOCK:
        if _CURRENT is None:
            return
        if _CURRENT.stop_reason and status == "finished":
            status = "stopped"
        if status == "finished" and _CURRENT.status.startswith("verification_"):
            status = _CURRENT.status
        if status == "finished" and _CURRENT.status in {"awaiting_approval", "stopping", "stopped"}:
            status = _CURRENT.status
        _CURRENT.status = status
        _persist(_CURRENT)
        audit_logger.log_task_end(
            _CURRENT.task_id,
            _CURRENT.batch_id,
            _CURRENT.thread_id,
            status,
            {
                "steps": _CURRENT.steps,
                "model_calls": _CURRENT.model_calls,
                "cost_yuan": round(_CURRENT.cost_yuan, 4),
                "stop_reason": _CURRENT.stop_reason,
                "tool_errors": _CURRENT.tool_errors,
                "verification_status": _CURRENT.verification_status,
            },
        )
        _CURRENT = None


def pause_task(status: str = "awaiting_approval") -> None:
    """把任务标记为挂起（等待人工确认），但保留租约外的状态。"""
    with _LOCK:
        if _CURRENT is None:
            return
        _CURRENT.status = status
        _persist(_CURRENT)


def current() -> TaskRuntime | None:
    with _LOCK:
        return _CURRENT


def current_batch_id() -> str:
    with _LOCK:
        return _CURRENT.batch_id if _CURRENT else ""


def current_card() -> TaskCard | None:
    with _LOCK:
        return TaskCard.from_dict(_CURRENT.card) if _CURRENT and _CURRENT.card else None


def current_card_dict() -> dict | None:
    with _LOCK:
        return dict(_CURRENT.card) if _CURRENT and _CURRENT.card else None


def stop_reason() -> str:
    """返回停止原因；空字符串表示没有急停。"""
    with _LOCK:
        if _CURRENT and _CURRENT.stop_reason:
            return _CURRENT.stop_reason
    return _stop_file_reason()


def request_stop(reason: str = "用户按下急停") -> None:
    """触发急停：写 STOP 文件，并标记当前任务。"""
    text = f"{reason}\n{datetime.now().isoformat(timespec='seconds')}\n"
    try:
        path = _stop_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        logger.error(f"[guard]写入急停文件失败：{exc}")
    with _LOCK:
        if _CURRENT:
            _CURRENT.stop_reason = reason
            _CURRENT.status = "stopping"
            _persist(_CURRENT)
    audit_logger.log_guard_event(
        "emergency_stop",
        reason=reason,
        batch_id=_CURRENT.batch_id if _CURRENT else "",
        task_id=_CURRENT.task_id if _CURRENT else "",
    )
    logger.warning(f"[guard]急停已触发：{reason}")


def clear_stop() -> None:
    """解除急停；下一次任务可以重新开始。"""
    try:
        _stop_file().unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(f"[guard]删除急停文件失败：{exc}")
    with _LOCK:
        if _CURRENT:
            _CURRENT.stop_reason = ""
            _CURRENT.status = "running"
            _persist(_CURRENT)
    audit_logger.log_guard_event(
        "emergency_stop_cleared",
        batch_id=_CURRENT.batch_id if _CURRENT else "",
        task_id=_CURRENT.task_id if _CURRENT else "",
    )
    logger.info("[guard]急停已解除")


def budget_reason() -> str:
    """检查步数/时长/费用/有效期，返回第一条超限说明。"""
    with _LOCK:
        task = _CURRENT
        if task is None:
            return ""
        if task.steps >= task.max_steps:
            return f"已达到单任务步数上限（{task.max_steps} 步）"
        elapsed = time.time() - task.started_at
        if elapsed >= task.max_minutes * 60:
            return f"已达到单任务时长上限（{task.max_minutes} 分钟）"
        if elapsed >= task.valid_minutes * 60:
            return f"任务卡已过期（有效期 {task.valid_minutes} 分钟）"
        if task.cost_yuan >= task.max_cost_yuan:
            return f"已达到单任务费用上限（{task.max_cost_yuan} 元，估算值）"
        return ""


def task_block_reason() -> str:
    """停止 + 预算的统一检查。"""
    return stop_reason() or budget_reason()


def mark_stopped(reason: str) -> None:
    """记录“因为预算/急停而停止”，供审计与界面展示。"""
    with _LOCK:
        task = _CURRENT
        if task is None:
            return
        already = bool(task.stop_reason)
        task.stop_reason = reason
        task.status = "stopped"
        _persist(task)
        batch_id, task_id = task.batch_id, task.task_id
    if not already:
        audit_logger.log_guard_event(
            "budget_stop",
            reason=reason,
            batch_id=batch_id,
            task_id=task_id,
        )


def record_tool_status(status: str) -> None:
    """记录一次工具最终状态；error 会让任务结束时不能判定“全部通过”。"""
    if str(status).lower() != "error":
        return
    with _LOCK:
        if _CURRENT is None:
            return
        _CURRENT.tool_errors += 1
        _persist(_CURRENT)


def mark_verification(status: str) -> None:
    """记录任务结束时的机器判据结论。

    `not_required` 表示这次没有配置机器判据（快速任务常见），按人工确认
    和操作记录验收；只有配置了判据却失败/未确认时才阻断 finished。
    """
    with _LOCK:
        if _CURRENT is None:
            return
        _CURRENT.verification_status = str(status or "missing")
        if _CURRENT.verification_status not in {"passed", "not_required"}:
            _CURRENT.status = f"verification_{_CURRENT.verification_status}"
        _persist(_CURRENT)


def consume_step() -> int:
    """工具真正执行前记一步；返回当前累计步数。"""
    with _LOCK:
        if _CURRENT is None:
            return 0
        _CURRENT.steps += 1
        _persist(_CURRENT)
        return _CURRENT.steps


def add_usage(input_tokens: int = 0, output_tokens: int = 0) -> None:
    """累计模型调用与 token；只有供应商返回 usage 时才计费。"""
    with _LOCK:
        if _CURRENT is None:
            return
        _CURRENT.model_calls += 1
        _CURRENT.input_tokens += max(0, int(input_tokens or 0))
        _CURRENT.output_tokens += max(0, int(output_tokens or 0))
        input_price, output_price = model_prices()
        _CURRENT.cost_yuan += (
            _CURRENT.input_tokens / 1_000_000 * input_price
            + _CURRENT.output_tokens / 1_000_000 * output_price
        )
        _persist(_CURRENT)


def _approval_path() -> Path:
    return state_dir() / _APPROVAL_FILE


def _persist_approval(payload: dict) -> None:
    """原子写入审批上下文；失败只告警，不影响本轮已批准的这次调用。"""
    path = _approval_path()
    temp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, path)
    except OSError as exc:
        logger.warning(f"[guard]保存审批上下文失败（已忽略）：{exc}")
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def _load_approval() -> dict:
    """读取落盘副本；过期或损坏一律当作没有，避免旧凭证被误用。"""
    path = _approval_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict) or not data.get("token"):
        return {}
    try:
        saved_at = float(data.get("saved_at") or 0)
    except (TypeError, ValueError):
        return {}
    if time.time() - saved_at > _APPROVAL_FILE_TTL_SECONDS:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return {}
    return data


def _approval_snapshot() -> dict:
    """内存优先；内存为空时读落盘副本（跨模块实例/跨热重载共享）。"""
    if _APPROVAL.get("token"):
        return _APPROVAL
    return _load_approval()


def set_approval_context(
    token: str = "",
    approver: str = "human",
    actions: list[dict] | None = None,
    *,
    thread_id: str = "",
    batch_id: str = "",
) -> None:
    """审批通过后暂存签名凭证与绑定信息，供执行前验签；不保存 PIN。"""
    normalized_actions = [
        {
            "name": str(item.get("name", "")),
            "args": dict(item.get("args")) if isinstance(item.get("args"), dict) else {},
        }
        for item in (actions or [])
        if isinstance(item, dict) and item.get("name")
    ]
    names = sorted({item["name"] for item in normalized_actions})
    payload = {
        "token": str(token or ""),
        "approver": str(approver or "human"),
        "names": names,
        "actions": normalized_actions,
        "thread_id": str(thread_id or ""),
        "batch_id": str(batch_id or ""),
        "saved_at": time.time(),
    }
    _APPROVAL.clear()
    _APPROVAL.update(payload)
    _persist_approval(payload)


def approval_context(tool_name: str = "") -> dict:
    data = _approval_snapshot()
    names = data.get("names")
    if names and tool_name and tool_name not in names:
        return {}
    return {
        "token": data.get("token", ""),
        "approver": data.get("approver", "agent"),
        "actions": list(data.get("actions") or []),
        "thread_id": data.get("thread_id", ""),
        "batch_id": data.get("batch_id", ""),
    }


def clear_approval_context() -> None:
    """清空审批上下文；同时删除落盘副本，避免下一轮误用旧凭证。"""
    _APPROVAL.clear()
    try:
        _approval_path().unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(f"[guard]删除审批上下文失败（已忽略）：{exc}")


def reset_for_tests() -> None:
    """测试用：清空进程内状态，不碰磁盘。"""
    global _CURRENT
    with _LOCK:
        _CURRENT = None
    _APPROVAL.clear()


def active_summary() -> dict:
    """给界面看的一行状态。"""
    with _LOCK:
        task = _CURRENT
        if task is None:
            return {"active": False}
        return {
            "active": True,
            "task_id": task.task_id,
            "batch_id": task.batch_id,
            "thread_id": task.thread_id,
            "steps": task.steps,
            "max_steps": task.max_steps,
            "model_calls": task.model_calls,
            "cost_yuan": round(task.cost_yuan, 4),
            "max_cost_yuan": task.max_cost_yuan,
            "elapsed_seconds": int(time.time() - task.started_at),
            "max_minutes": task.max_minutes,
            "status": task.status,
            "stop_reason": task.stop_reason,
            "verification_status": task.verification_status,
        }
