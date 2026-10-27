"""可回滚边界（P0-3 的验收口径）。

批次回滚只覆盖由文件工具产生的、可确定性撤销的改动：
- 写入/覆盖/追加（有备份或可截断）；
- 建目录；
- 移动/复制；
- 删除到回收区。

以下不在回滚范围：
- `run_command` 内部产生的文件/注册表/网络/应用状态；
- `open_path` 启动程序后的状态；
- `computer_use` 的界面/应用内状态；
- 永久删除（不可逆）；
- 超过 10 MB 的覆盖（没有备份）。
"""

ROLLBACKABLE_TOOLS = frozenset(
    {
        "write_text_file",
        "create_directory",
        "move_path",
        "copy_path",
        "delete_path",
    }
)

NON_ROLLBACKABLE_TOOLS = frozenset(
    {
        "run_command",
        "open_path",
        "computer_use",
    }
)

ROLLBACK_BOUNDARY_TEXT = (
    "可回滚：写入/覆盖/追加、建目录、移动、复制、删除到回收区；"
    "不可回滚：命令、打开程序、GUI 操作、永久删除、超过 10 MB 的覆盖、应用内状态。"
)


def is_rollbackable(tool_name: str, args: dict | None = None) -> bool:
    """判断一次工具调用是否在批次回滚边界内。"""
    if tool_name == "delete_path" and (args or {}).get("permanent"):
        return False
    return tool_name in ROLLBACKABLE_TOOLS


def rollback_note(tool_name: str, args: dict | None = None) -> str:
    """给确认卡片/任务卡用的边界说明。"""
    if is_rollbackable(tool_name, args):
        return "在批次回滚范围内"
    return "不在批次回滚范围内；请把这一步当成需要人工核对的不可撤销/难逆操作"
