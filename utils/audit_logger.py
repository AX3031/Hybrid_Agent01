"""结构化审计日志（P0-6）。

每次实际执行的工具调用写一行 JSONL，便于事后回答三个问题：

- 发生了什么：工具、参数（脱敏后）、结果摘要、状态；
- 谁批准的：`approver` 与审批凭证摘要（不记录 PIN）；
- 在哪个任务/批次里：`trace_id`、`batch_id`、`step_no`、`task_id`。

另外记录任务开始/结束、审批、急停、回滚等关键事件。文件位置：
`logs/audit_YYYYMMDD.jsonl`。审计写入失败不能影响主流程，因此异常会被
静默吞掉。

注意：这不是 WORM 存储，Agent 理论上仍可通过其他手段篡改工作区文件；
小企业版的定位是“事后能看出来”，不是“法律级不可篡改”。
"""

import json
from datetime import datetime
from pathlib import Path

from utils.path_tool import get_abs_path

_AUDIT_DIR = Path(get_abs_path("logs"))
_POLICY_VERSION = "small-business-p0-v1"


def _append(record: dict) -> None:
    try:
        _AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        path = _AUDIT_DIR / f"audit_{datetime.now():%Y%m%d}.jsonl"
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def _base_record(event: str, **fields) -> dict:
    return {
        "time": datetime.now().isoformat(timespec="seconds"),
        "event": event,
        "policy_version": _POLICY_VERSION,
        **fields,
    }


def log_tool_call(
    session_id: str,
    tool_name: str,
    args: dict,
    result: str,
    status: str = "success",
    *,
    batch_id: str = "",
    task_id: str = "",
    step_no: int = 0,
    reversibility: str = "",
    approver: str = "agent",
    approval_token: str = "",
    trace_id: str = "",
) -> None:
    """追加一条工具调用审计记录。"""
    _append(
        _base_record(
            "tool_call",
            trace_id=trace_id or task_id,
            task_id=task_id,
            batch_id=batch_id,
            step_no=int(step_no or 0),
            session_id=session_id or "",
            tool=tool_name,
            args=args,
            status=status,
            reversibility=reversibility,
            approver=approver,
            approval_token=approval_token,
            result=str(result or "")[:500],
        )
    )


def log_task_start(task_id: str, batch_id: str, thread_id: str, card: dict | None) -> None:
    """任务开始时写一条快照：谁在什么时候授权了什么范围。"""
    _append(
        _base_record(
            "task_start",
            trace_id=task_id,
            task_id=task_id,
            batch_id=batch_id,
            session_id=thread_id,
            contract=card or {},
            approver="human",
        )
    )


def log_task_end(task_id: str, batch_id: str, thread_id: str, status: str, summary: dict | None = None) -> None:
    _append(
        _base_record(
            "task_end",
            trace_id=task_id,
            task_id=task_id,
            batch_id=batch_id,
            session_id=thread_id,
            status=status,
            summary=summary or {},
        )
    )


def log_approval(
    thread_id: str,
    batch_id: str,
    decision: str,
    actions: list[dict],
    *,
    token: str = "",
    approver: str = "human",
    task_id: str = "",
) -> None:
    """记录一次人工审批；只记凭证摘要，不记 PIN。"""
    _append(
        _base_record(
            "approval",
            trace_id=task_id,
            task_id=task_id,
            batch_id=batch_id,
            session_id=thread_id,
            decision=decision,
            actions=actions,
            approver=approver,
            approval_token=token,
        )
    )


def log_guard_event(event: str, **fields) -> None:
    """急停、回滚、预算超限等守卫层事件。"""
    _append(_base_record(event, **fields))
