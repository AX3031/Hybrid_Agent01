"""跑一遍离线验证用例，并把证据写成 JSON。

用法（在项目根执行）：

    .venv\\Scripts\\python.exe scripts\\verify_agent_resilience.py
    # 或
    uv run python scripts/verify_agent_resilience.py

产物：`docs/agent-resilience/evidence/verify-YYYYmmdd-HHMMSS.json`

为什么要有这个脚本：
- 单元测试只给“红/绿”，不留下可归档的证据；
- 方法论要求“每个阶段有指标快照 + 可回滚点”，这里把用例明细、
  运行环境、Git commit 一起存档，方便下次改造对比。
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
import time
import unittest
from datetime import datetime
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = ROOT / "docs" / "agent-resilience" / "evidence"

# 直接 python scripts/xxx.py 运行时，sys.path[0] 是 scripts/，需要补项目根
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class RecordingResult(unittest.TextTestResult):
    """在标准输出之外，额外记录每条用例的状态与耗时。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records: list[dict] = []
        self._started_at: dict[str, float] = {}

    def startTest(self, test):  # noqa: N802 - unittest 命名约定
        self._started_at[test.id()] = time.perf_counter()
        super().startTest(test)

    def _record(self, test, status: str, detail: str = "") -> None:
        started = self._started_at.get(test.id())
        elapsed = round(time.perf_counter() - started, 4) if started else None
        self.records.append(
            {"id": test.id(), "status": status, "duration_s": elapsed, "detail": detail}
        )

    def addSuccess(self, test):  # noqa: N802
        self._record(test, "ok")
        super().addSuccess(test)

    def addFailure(self, test, err):  # noqa: N802
        self._record(test, "failed", self._exc_info_to_string(err, test).strip().splitlines()[-1])
        super().addFailure(test, err)

    def addError(self, test, err):  # noqa: N802
        self._record(test, "error", self._exc_info_to_string(err, test).strip().splitlines()[-1])
        super().addError(test, err)

    def addSkip(self, test, reason):  # noqa: N802
        self._record(test, "skipped", reason)
        super().addSkip(test, reason)


def _version(package: str) -> str:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return "not-installed"


def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except Exception:  # noqa: BLE001 - 证据不因为 Git 不可用而失败
        return "unknown"


def main() -> int:
    started = time.perf_counter()
    suite = unittest.defaultTestLoader.discover(start_dir=str(ROOT / "tests"), top_level_dir=str(ROOT))
    runner = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult)
    result = runner.run(suite)
    duration = round(time.perf_counter() - started, 2)

    summary = {
        "total": result.testsRun,
        "ok": sum(1 for r in result.records if r["status"] == "ok"),
        "failed": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "duration_s": duration,
    }
    payload = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "git_commit": _git_commit(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "langchain": _version("langchain"),
        "langgraph": _version("langgraph"),
        "summary": summary,
        "cases": result.records,
    }

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = EVIDENCE_DIR / f"verify-{stamp}.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== 验证摘要 ===")
    print(
        f"用例 {summary['total']} 条 | 通过 {summary['ok']} | 失败 {summary['failed']} "
        f"| 异常 {summary['errors']} | 跳过 {summary['skipped']} | 耗时 {duration}s"
    )
    print(f"证据已写入：{target.relative_to(ROOT)}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
