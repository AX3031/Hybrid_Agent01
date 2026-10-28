"""同步 docs/_generated/facts.md 与当前代码/配置。

用法（项目根）：

    uv run python scripts/sync_doc_facts.py          # 写入
    uv run python scripts/sync_doc_facts.py --check  # 只校验，不写
    uv run python scripts/sync_doc_facts.py --stdout # 打印生成内容

为什么需要它：

- README/架构文档里的工具数、动作数、用例数、预算、模型名很容易随代码漂移；
- 这些数字由 `scripts/doc_facts.py` 从代码/配置派生，不靠手写；
- `tests/test_docs_consistency.py` 会在 CI/本地测试里跑 `--check`，
  发现文档没同步就红灯。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.doc_facts import check_facts_file, render_facts_markdown, write_facts_file  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="同步自动生成的文档事实")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="只校验，不写文件")
    group.add_argument("--stdout", action="store_true", help="把生成内容打印到标准输出")
    args = parser.parse_args()

    if args.stdout:
        print(render_facts_markdown())
        return 0

    if args.check:
        ok, message = check_facts_file()
        print(message)
        return 0 if ok else 1

    target = write_facts_file()
    print(f"已生成：{target.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
