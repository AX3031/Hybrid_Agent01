"""设置第二主体审批口令（PIN）。

用法：

    uv run python scripts/setup_approval_pin.py
    uv run python scripts/setup_approval_pin.py --status
    uv run python scripts/setup_approval_pin.py --force

安全约定：

- 口令只保存 PBKDF2 哈希，不保存明文；
- 口令目录默认在 `%PROGRAMDATA%\\OSGuiAgent\\approval`，Agent 工具无法读写；
- 不要把口令写进 .env、提示词、README 或聊天记录。
"""

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.guard import approval  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="设置或查看审批口令")
    parser.add_argument("--status", action="store_true", help="只查看当前是否已设置")
    parser.add_argument("--force", action="store_true", help="覆盖已有的审批口令")
    args = parser.parse_args()

    print(f"审批目录：{approval.approval_dir()}")
    if args.status:
        print(approval.pin_status_text())
        return 0

    if approval.pin_configured() and not args.force:
        print("审批口令已经设置。要重设请加 --force。")
        return 1

    first = getpass.getpass("输入新的审批口令（至少 6 位，输入时不显示）：")
    second = getpass.getpass("再输入一次确认：")
    if first != second:
        print("两次输入不一致，未修改。")
        return 1

    try:
        approval.set_pin(first)
    except ValueError as exc:
        print(f"设置失败：{exc}")
        return 1

    print("审批口令已设置（只保存哈希）。")
    print("请自己记住；忘记后只能重新运行本脚本重设，旧口令无法找回。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
