"""结构化审计日志。

每次实际执行的工具调用写一行 JSONL，便于事后追溯：
- 时间、会话 ID、工具名、参数、状态、结果摘要。
文件位置：`logs/audit_YYYYMMDD.jsonl`。

审计写入失败不能影响主流程，因此异常会被静默吞掉。
"""

import json
from datetime import datetime
from pathlib import Path

from utils.path_tool import get_abs_path

_AUDIT_DIR = Path(get_abs_path("logs"))


def log_tool_call(
    session_id: str,
    tool_name: str,
    args: dict,
    result: str,
    status: str = "success",
) -> None:
    """追加一条工具调用审计记录。"""
    try:
        _AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        path = _AUDIT_DIR / f"audit_{datetime.now():%Y%m%d}.jsonl"
        record = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "session_id": session_id or "",
            "tool": tool_name,
            "args": args,
            "status": status,
            "result": str(result or "")[:500],
        }
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass
