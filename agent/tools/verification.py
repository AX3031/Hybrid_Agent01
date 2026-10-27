"""工具结果的确定性后置校验。

为什么要做这件事：

- 工具返回“已写入 / 已删除 / 已移动”只是它自己的说法。磁盘满、路径被占用、
  回收区移动失败等情况，都可能让结果与事实不一致；
- 模型没有眼睛，工具说什么它就信什么；一旦它把“已写入”写进回答，用户就被误导；
- 与其让模型自我怀疑，不如用确定性的文件系统检查给出结论。

设计约束：

- **只验证“声称成功”的结果**（工具自己已经报错时不再重复校验）；
- **只验证能确定性核对的东西**（写文件、建目录、移动、复制、删除）；
- **校验本身绝不抛异常**：查不了就跳过，不能因为校验把正常流程打断。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from langchain_core.messages import ToolMessage

from agent.tools.safety import expand_path, in_workspace
from utils.logger_handler import logger

# 逐字比对时允许读取的最大文件；超过就只核对存在性与大小
_MAX_READ_BYTES = 5 * 1024 * 1024

# 工具“声称成功”的开头，用于避免对失败结果做二次校验
_SUCCESS_PREFIXES: dict[str, tuple[str, ...]] = {
    "write_text_file": ("已写入", "已追加"),
    "create_directory": ("目录已就绪",),
    "move_path": ("已移动",),
    "copy_path": ("已复制",),
    "delete_path": ("已删除", "已永久删除", "已移到回收区"),
}


@dataclass(frozen=True)
class VerificationResult:
    """一次校验的结论：是否适用、是否通过、给人看的一句话说明。"""

    checked: bool
    ok: bool = True
    note: str = ""

    @classmethod
    def skip(cls) -> "VerificationResult":
        return cls(checked=False)


def _normalize_newlines(text: str) -> str:
    """Windows 文本写入会把 \\n 转成 \\r\\n，比对前统一成 \\n。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _existing_file_stats(path: Path) -> str:
    try:
        return f"文件存在，{path.stat().st_size} 字节"
    except OSError:
        return "文件存在"


def _check_write(args: dict, _result: str) -> VerificationResult:
    target = expand_path(str(args.get("path", "")))
    content = str(args.get("content", ""))
    mode = str(args.get("mode") or "write")
    if not in_workspace(target):
        return VerificationResult.skip()

    if not target.exists():
        return VerificationResult(checked=True, ok=False, note=f"目标文件不存在：{target}")
    if not target.is_file():
        return VerificationResult(checked=True, ok=False, note=f"目标不是文件：{target}")

    if not content:
        return VerificationResult(checked=True, ok=True, note=_existing_file_stats(target))
    if target.stat().st_size > _MAX_READ_BYTES:
        return VerificationResult(
            checked=True, ok=True, note=f"{_existing_file_stats(target)}（文件较大，未逐字比对）"
        )

    try:
        actual = _normalize_newlines(target.read_text(encoding="utf-8", errors="replace"))
    except OSError as exc:
        return VerificationResult(checked=True, ok=False, note=f"回读文件失败：{exc}")

    expected = _normalize_newlines(content)
    if mode == "append":
        passed = actual.endswith(expected)
        action = "追加内容"
    else:
        passed = actual == expected
        action = "文件内容"
    if passed:
        return VerificationResult(
            checked=True, ok=True, note=f"{action}与写入参数一致，{_existing_file_stats(target)}"
        )
    return VerificationResult(
        checked=True, ok=False, note=f"{action}与写入参数不一致（可能被换行或编码改写）"
    )


def _check_create_directory(args: dict, _result: str) -> VerificationResult:
    target = expand_path(str(args.get("path", "")))
    if not in_workspace(target):
        return VerificationResult.skip()
    if target.is_dir():
        return VerificationResult(checked=True, ok=True, note=f"目录存在：{target}")
    return VerificationResult(checked=True, ok=False, note=f"目录不存在：{target}")


def _resolve_destination(src: Path, dst: Path) -> Path:
    """目标路径是已存在目录时，工具会把源放进该目录下，校验同样要跟着走。"""
    return dst / src.name if dst.is_dir() else dst


def _check_move(args: dict, _result: str) -> VerificationResult:
    src = expand_path(str(args.get("source", "")))
    dst = expand_path(str(args.get("destination", "")))
    if not (in_workspace(src) and in_workspace(dst)):
        return VerificationResult.skip()

    actual_dst = _resolve_destination(src, dst)
    if src.exists():
        return VerificationResult(checked=True, ok=False, note=f"源路径仍然存在，移动可能没有生效：{src}")
    if not actual_dst.exists():
        return VerificationResult(checked=True, ok=False, note=f"目标路径不存在：{actual_dst}")
    return VerificationResult(checked=True, ok=True, note=f"源已消失且目标存在：{actual_dst}")


def _check_copy(args: dict, _result: str) -> VerificationResult:
    src = expand_path(str(args.get("source", "")))
    dst = expand_path(str(args.get("destination", "")))
    if not (in_workspace(src) and in_workspace(dst)):
        return VerificationResult.skip()

    actual_dst = _resolve_destination(src, dst)
    if actual_dst.exists():
        return VerificationResult(checked=True, ok=True, note=f"目标存在：{actual_dst}")
    return VerificationResult(checked=True, ok=False, note=f"目标不存在：{actual_dst}")


def _check_delete(args: dict, _result: str) -> VerificationResult:
    target = expand_path(str(args.get("path", "")))
    if not in_workspace(target):
        return VerificationResult.skip()
    if target.exists():
        return VerificationResult(checked=True, ok=False, note=f"路径仍然存在，删除可能没有生效：{target}")
    return VerificationResult(checked=True, ok=True, note="原路径已不存在（永久删除或已移入回收区）")


_CHECKERS: dict[str, Callable[[dict, str], VerificationResult]] = {
    "write_text_file": _check_write,
    "create_directory": _check_create_directory,
    "move_path": _check_move,
    "copy_path": _check_copy,
    "delete_path": _check_delete,
}


def verify_tool_outcome(tool_name: str, tool_args: dict | None, result_content: Any) -> VerificationResult:
    """核对工具“声称成功”的结果；不适用时返回 `checked=False`。"""
    checker = _CHECKERS.get(tool_name)
    if checker is None:
        return VerificationResult.skip()
    if not isinstance(result_content, str) or not result_content.strip():
        return VerificationResult.skip()
    if not result_content.startswith(_SUCCESS_PREFIXES[tool_name]):
        return VerificationResult.skip()

    try:
        return checker(tool_args or {}, result_content)
    except Exception as exc:  # noqa: BLE001 - 校验失败不应打断工具调用
        logger.warning(f"[verification]{tool_name} 结果校验执行失败，已跳过：{exc}")
        return VerificationResult.skip()


def enrich_tool_message(tool_name: str, tool_args: dict | None, result: Any) -> Any:
    """把校验结论附加到 ToolMessage 上；非 ToolMessage 或不适用时原样返回。

    校验未通过时把 `status` 改成 `"error"`，这样：
    - 模型能看到“工具说成功、但核对不上”，不会照抄成功结论；
    - 界面「操作记录」会显示“未完成”，审计日志也会记 error。
    """
    if not isinstance(result, ToolMessage):
        return result

    outcome = verify_tool_outcome(tool_name, tool_args, result.content)
    if not outcome.checked:
        return result

    if outcome.ok:
        content = f"{result.content}\n\n[结果校验] 已核对：{outcome.note}"
        return result.model_copy(update={"content": content})

    content = (
        f"{result.content}\n\n[结果校验未通过] {outcome.note}\n"
        "工具声称成功，但后置检查没有通过；请先确认目标的真实状态，再决定是否重试。"
    )
    return result.model_copy(update={"content": content, "status": "error"})
