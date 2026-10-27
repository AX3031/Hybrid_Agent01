"""守卫策略：只读、任务卡范围、可逆性分级（P0-1 / P0-2 / P0-4 的共用部分）。

这一层全部是确定性判断，不依赖模型“自觉”：

- `check_read_only`：只读模式下哪些工具/桌面动作必须拒绝；
- `check_task_card`：动作类别、输入/输出目录是否越界；
- `reversibility_of`：给每个动作标 R0–R3，决定是否需要第二主体；
- `preflight_reason`：把停止、只读、任务卡、预算合并成一次检查。
"""

from pathlib import Path

from agent.guard import state
from agent.guard.config import read_only_enabled
from agent.guard.task_card import (
    ACTION_COMMAND,
    ACTION_COPY,
    ACTION_DELETE,
    ACTION_DESKTOP,
    ACTION_MOVE,
    ACTION_NETWORK,
    ACTION_OPEN,
    ACTION_READ,
    ACTION_WRITE,
    TaskCard,
)

# 只读桌面动作：不改变本机状态，只读模式下允许
DESKTOP_READ_ONLY_ACTIONS = {
    "screen_size",
    "locate_image",
    "wait",
    "screenshot",
    "clipboard_get",
    "app_list",
}

# 任务卡里的“发送/提交/付款”是语义禁令，不是工具类别；GUI 无法从坐标/文字
# 判断某次点击是否会真正发送或付款。因此只要卡里保留了这些词，就保守拒绝
# 所有会触发副作用的桌面动作，避免给出“已经禁止”的虚假安全感。
GUI_SEMANTIC_FORBIDDEN = {"发送", "提交", "付款"}

_ACTION_BY_TOOL = {
    "get_system_info": ACTION_READ,
    "list_processes": ACTION_READ,
    "list_directory": ACTION_READ,
    "read_text_file": ACTION_READ,
    "search_files": ACTION_READ,
    "search_text": ACTION_READ,
    "write_text_file": ACTION_WRITE,
    "create_directory": ACTION_WRITE,
    "move_path": ACTION_MOVE,
    "copy_path": ACTION_COPY,
    "delete_path": ACTION_DELETE,
    "run_command": ACTION_COMMAND,
    "open_path": ACTION_OPEN,
    "computer_use": ACTION_DESKTOP,
    "web_search_baidu": ACTION_NETWORK,
    "fetch_external_data": ACTION_READ,
    "get_weather": ACTION_READ,
    "get_user_location": ACTION_READ,
    "get_user_id": ACTION_READ,
    "get_current_month": ACTION_READ,
    "fill_context_for_report": ACTION_READ,
}

# 会产生本机副作用的工具
WRITE_TOOLS = {
    "write_text_file",
    "create_directory",
    "move_path",
    "copy_path",
    "delete_path",
    "run_command",
    "open_path",
}

_READ_PATH_TOOLS = {"list_directory", "read_text_file", "search_files", "search_text"}
_WRITE_PATH_TOOLS = {"write_text_file", "create_directory"}
_BOTH_PATH_TOOLS = {"move_path", "copy_path", "delete_path", "open_path"}


def tool_action(tool_name: str, args: dict | None = None) -> str | None:
    """把工具映射成任务卡里的动作类别；未知工具返回 None（不额外限制）。"""
    return _ACTION_BY_TOOL.get(tool_name)


def reversibility_of(tool_name: str, args: dict | None = None) -> str:
    """动作可逆性分级 R0–R3。

    - R0：只读，不改变外部状态；
    - R1：可确定性撤销（回收区、覆盖前备份、批次日志）；
    - R2：难逆，需要人工审批口令（命令、打开程序、桌面输入）；
    - R3：不可逆，必须人工审批且**不能**自动提交（永久删除）。
    """
    args = args or {}
    if tool_name == "delete_path":
        return "R3" if args.get("permanent") else "R1"
    if tool_name == "run_command":
        return "R2"
    if tool_name == "open_path":
        return "R2"
    if tool_name == "computer_use":
        action = str(args.get("action", ""))
        return "R0" if action in DESKTOP_READ_ONLY_ACTIONS else "R2"
    if tool_name in {"write_text_file", "create_directory", "move_path", "copy_path"}:
        return "R1"
    if tool_name in WRITE_TOOLS:
        return "R1"
    return "R0"


def requires_second_subject(tool_name: str, args: dict | None = None) -> bool:
    """R2/R3 动作需要人工持有的审批口令。"""
    return reversibility_of(tool_name, args) in {"R2", "R3"}


def requires_task_card(tool_name: str, args: dict | None = None) -> bool:
    """会修改本机的动作必须有任务卡；只读问答可以不带卡。"""
    if tool_name in WRITE_TOOLS:
        return True
    if tool_name == "computer_use":
        action = str((args or {}).get("action", ""))
        return action not in DESKTOP_READ_ONLY_ACTIONS
    return False


def check_read_only(tool_name: str, args: dict | None = None) -> str:
    """只读总开关；通过时返回空字符串。"""
    if not read_only_enabled():
        return ""
    if tool_name in WRITE_TOOLS:
        return (
            f"当前处于只读模式，已拒绝有副作用的工具 `{tool_name}`。"
            "如需写入，请先在侧边栏关闭「只读模式」，或在 config/agent.yml 里把 read_only 改为 false。"
        )
    if tool_name == "computer_use":
        action = str((args or {}).get("action", ""))
        if action not in DESKTOP_READ_ONLY_ACTIONS:
            return (
                f"当前处于只读模式，已拒绝桌面动作 `{action}`。"
                "只读模式只允许查看屏幕尺寸、查找图片、等待、截图、读取剪贴板和应用列表。"
            )
    return ""


def _workspace_path(raw: str) -> Path | None:
    if not raw:
        return None
    try:
        from agent.tools.safety import expand_path

        return expand_path(raw)
    except Exception:  # noqa: BLE001 - 路径解析失败交给工具本身报错
        return None


def _under_any(path: Path, roots: list[Path]) -> bool:
    if not roots:
        return True
    from agent.tools.safety import workspace_root_of

    return any(workspace_root_of(path) is not None and _is_under(path, root) for root in roots)


def _is_under(path: Path, root: Path) -> bool:
    import os

    child = os.path.normcase(os.path.realpath(os.path.abspath(str(path))))
    parent = os.path.normcase(os.path.realpath(os.path.abspath(str(root))))
    return child == parent or child.startswith(parent + os.sep)


def _input_roots(card: TaskCard) -> list[Path]:
    return [path for raw in card.input_scope if (path := _workspace_path(raw))]


def _output_root(card: TaskCard) -> Path | None:
    return _workspace_path(card.output_location) if card.output_location else None


def _check_read_scope(card: TaskCard, raw: str, label: str) -> str:
    roots = _input_roots(card)
    if not roots or not raw:
        return ""
    path = _workspace_path(raw)
    if path is None:
        return ""
    if not _under_any(path, roots):
        return f"任务卡规定只能读取 {card.input_scope}，`{label}` 指向 {path}，已拒绝。"
    return ""


def _check_write_scope(card: TaskCard, raw: str, label: str) -> str:
    root = _output_root(card)
    if root is None or not raw:
        return ""
    path = _workspace_path(raw)
    if path is None:
        return ""
    if not _is_under(path, root):
        return f"任务卡规定只能写入 {card.output_location}，`{label}` 指向 {path}，已拒绝。"
    return ""


def check_task_card(card: TaskCard, tool_name: str, args: dict | None = None) -> str:
    """按任务卡检查动作类别与目录范围；通过时返回空字符串。"""
    args = args or {}
    action = tool_action(tool_name, args)
    if action:
        allowed = card.allowed_actions
        if allowed and action not in allowed:
            return f"任务卡没有允许「{action}」类动作，已拒绝工具 `{tool_name}`。"
        if action in card.forbidden_actions:
            return f"任务卡明确禁止「{action}」类动作，已拒绝工具 `{tool_name}`。"

    if tool_name in _READ_PATH_TOOLS:
        raw = str(args.get("path") or args.get("root") or "")
        return _check_read_scope(card, raw, tool_name)

    if tool_name in _WRITE_PATH_TOOLS:
        return _check_write_scope(card, str(args.get("path", "")), tool_name)

    if tool_name == "move_path":
        source_error = _check_read_scope(card, str(args.get("source", "")), "source")
        if source_error:
            return source_error
        return _check_write_scope(card, str(args.get("destination", "")), "destination")

    if tool_name == "copy_path":
        source_error = _check_read_scope(card, str(args.get("source", "")), "source")
        if source_error:
            return source_error
        return _check_write_scope(card, str(args.get("destination", "")), "destination")

    if tool_name in {"delete_path", "open_path"}:
        raw = str(args.get("path", ""))
        if raw.startswith(("http://", "https://")):
            return ""
        path = _workspace_path(raw)
        roots = _input_roots(card)
        output_root = _output_root(card)
        in_input = bool(path and roots and _under_any(path, roots))
        in_output = bool(path and output_root and _is_under(path, output_root))
        if roots and path and not in_input and not in_output:
            return f"任务卡规定只能操作输入范围 {card.input_scope} 或输出位置 {card.output_location or '（未设置）'}，已拒绝：{path}"
        if output_root and path and not roots and not in_output:
            return f"任务卡规定只能操作输出位置 {card.output_location}，已拒绝：{path}"
        return ""

    if tool_name == "run_command":
        raw = str(args.get("cwd") or "")
        scope_roots = _input_roots(card)
        output_root = _output_root(card)
        if not raw and (scope_roots or output_root):
            # 未指定 cwd 时 run_command 默认在第一个工作区根执行；
            # 如果任务卡限定了范围，必须让模型显式给一个范围内的 cwd。
            from agent.tools.safety import workspace_roots

            default_roots = workspace_roots()
            default = default_roots[0] if default_roots else None
            if default is not None:
                in_scope = bool(scope_roots and _under_any(default, scope_roots))
                in_output = bool(output_root and _is_under(default, output_root))
                if not (in_scope or in_output):
                    return (
                        "任务卡限定了输入/输出范围，但 run_command 没有指定 cwd；"
                        "请在范围内的目录执行，或显式传入 cwd。"
                    )
            return ""
        if raw:
            input_error = _check_read_scope(card, raw, "cwd")
            if input_error:
                output_root = _output_root(card)
                path = _workspace_path(raw)
                if not (output_root and path and _is_under(path, output_root)):
                    return input_error
        return ""

    if tool_name == "computer_use":
        action = str(args.get("action", ""))
        if action not in DESKTOP_READ_ONLY_ACTIONS:
            semantic = [item for item in card.forbidden_actions if item in GUI_SEMANTIC_FORBIDDEN]
            if semantic:
                return (
                    f"任务卡禁止了「{'、'.join(semantic)}」，但系统无法判断一次点击/输入"
                    "是否会触发发送、提交或付款；为避免虚假保证，已拒绝该 GUI 输入类动作。"
                    "如果确实需要 GUI 输入，请从任务卡的禁止动作里移除这些语义词，"
                    "并保留人工确认 + PIN。"
                )
        if action == "locate_image":
            raw = str(args.get("image_path", ""))
            if raw:
                # locate_image 的模板图允许放在输入范围或输出位置
                path = _workspace_path(raw)
                roots = _input_roots(card)
                output_root = _output_root(card)
                if roots and path and not _under_any(path, roots):
                    if not (output_root and _is_under(path, output_root)):
                        return f"任务卡规定模板图只能放在输入范围 {card.input_scope}，已拒绝：{path}"
        return ""

    return ""


def preflight_reason(tool_name: str, args: dict | None = None) -> str:
    """工具执行前的统一检查：停止 → 只读 → 任务卡 → 预算。"""
    reason = state.stop_reason()
    if reason:
        return f"任务已停止：{reason}。请先解除急停，再重新发起任务。"
    reason = check_read_only(tool_name, args)
    if reason:
        return reason
    card = state.current_card()
    if card is None and requires_task_card(tool_name, args):
        return (
            f"工具 `{tool_name}` 会修改本机，必须先启用任务卡"
            "（目标 / 成功判据 / 允许范围 / 预算）。"
            "请到侧边栏「任务卡」填写并启用后重试；只读问答不需要任务卡。"
        )
    if card:
        reason = check_task_card(card, tool_name, args)
        if reason:
            return reason
    reason = state.budget_reason()
    if reason:
        return reason
    return ""
