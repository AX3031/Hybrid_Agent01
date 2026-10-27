"""任务卡的机器判据（P0-5 的“可验收”部分）。

自然语言判据只能给模型看，不能证明任务真的完成了。这里提供一组
**确定性的、可执行的**检查，任务结束后由代码逐条核对：

- `exists|<path>`：路径存在；
- `absent|<path>`：路径不存在；
- `file_count|<dir>|<期望数量>`：递归统计目录下文件数；
- `file_size|<file>|<字节数>`：文件大小精确匹配；
- `sha256|<file>|<64位十六进制>`：文件哈希匹配（5 MB 内）；
- `text_contains|<file>|<文字>`：文件包含文字；
- `text_not_contains|<file>|<文字>`：文件不包含文字。

路径必须落在当前工作区域内；解析失败或无法核对时返回 `confirmed=False`，
界面必须显示“未确认”，不能当成“通过”。
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_SUPPORTED = {
    "exists",
    "absent",
    "file_count",
    "file_size",
    "sha256",
    "text_contains",
    "text_not_contains",
}
_MAX_COUNT_FILES = 50_000
_MAX_HASH_BYTES = 5 * 1024 * 1024
_MAX_TEXT_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class MachineCheck:
    """一条已经解析、路径已经规范化的机器判据。"""

    kind: str
    path: str
    expected: str = ""
    raw: str = ""

    def describe(self) -> str:
        if self.kind in {"exists", "absent"}:
            return f"{self.kind} {self.path}"
        return f"{self.kind} {self.path} = {self.expected}"


def _expand_and_validate(raw_path: str) -> tuple[str, str]:
    from agent.tools.safety import expand_path, in_workspace

    try:
        path = expand_path(raw_path)
    except OSError:
        return "", f"路径无法解析：{raw_path}"
    if not in_workspace(path):
        return "", f"机器判据路径不在工作区域内：{path}"
    return str(path), ""


def parse_machine_check(line: str) -> tuple[MachineCheck | None, str]:
    """解析一行判据；返回 `(check, "")` 或 `(None, 错误)`。"""
    raw = str(line).strip()
    if not raw:
        return None, "机器判据不能为空"
    parts = raw.split("|")
    kind = parts[0].strip().lower()
    if kind not in _SUPPORTED:
        return None, f"不支持的机器判据类型：{parts[0]}"
    if len(parts) < 2 or not parts[1].strip():
        return None, f"机器判据缺少路径：{raw}"
    path, error = _expand_and_validate(parts[1].strip())
    if error:
        return None, error

    expected = "|".join(parts[2:]).strip()
    if kind in {"exists", "absent"}:
        if len(parts) > 2 and any(part.strip() for part in parts[2:]):
            return None, f"{kind} 不需要期望值：{raw}"
        return MachineCheck(kind=kind, path=path, raw=raw), ""

    if not expected:
        return None, f"机器判据缺少期望值：{raw}"
    if kind == "file_count":
        if not re.fullmatch(r"\d+(?:-\d+)?", expected):
            return None, f"file_count 期望值应为数量或 最小-最大：{raw}"
        if "-" in expected:
            low, high = (int(item) for item in expected.split("-", 1))
            if low > high:
                return None, f"file_count 的最小值不能大于最大值：{raw}"
    elif kind == "file_size":
        if not expected.isdigit():
            return None, f"file_size 期望值应为字节数：{raw}"
    elif kind == "sha256":
        if not _SHA256_RE.fullmatch(expected):
            return None, f"sha256 期望值应为 64 位十六进制：{raw}"
    elif kind == "text_contains" and not expected:
        return None, f"text_contains 缺少要查找的文字：{raw}"
    elif kind == "text_not_contains" and not expected:
        return None, f"text_not_contains 缺少要排除的文字：{raw}"

    return MachineCheck(kind=kind, path=path, expected=expected, raw=raw), ""


def validate_machine_checks(lines: list[str]) -> list[str]:
    """只校验格式与路径，不要求目标现在已经存在；返回错误列表。"""
    errors: list[str] = []
    for line in lines or []:
        _, error = parse_machine_check(line)
        if error:
            errors.append(error)
    return errors


def _result(check: MachineCheck, ok: bool | None, confirmed: bool, note: str) -> dict:
    return {
        "check": check.raw or check.describe(),
        "kind": check.kind,
        "path": check.path,
        "expected": check.expected,
        "ok": ok,
        "confirmed": confirmed,
        "note": note,
    }


def evaluate_machine_check(check: MachineCheck) -> dict:
    """执行一条判据；读不到/无法确定时返回 confirmed=False。"""
    path = Path(check.path)
    try:
        if check.kind == "exists":
            return _result(check, path.exists(), True, "存在" if path.exists() else "不存在")
        if check.kind == "absent":
            exists = path.exists()
            return _result(check, not exists, True, "不存在" if not exists else "仍然存在")
        if check.kind == "file_size":
            if not path.is_file():
                return _result(check, False, True, "目标不是文件")
            size = path.stat().st_size
            return _result(check, size == int(check.expected), True, f"实际 {size} 字节")
        if check.kind == "sha256":
            if not path.is_file():
                return _result(check, False, True, "目标不是文件")
            if path.stat().st_size > _MAX_HASH_BYTES:
                return _result(check, None, False, "文件超过 5 MB，未计算哈希")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            return _result(check, digest.lower() == check.expected.lower(), True, f"实际 {digest}")
        if check.kind == "file_count":
            if not path.is_dir():
                return _result(check, False, True, "目标不是目录")
            count = 0
            for _root, _dirs, files in os.walk(path):
                count += len(files)
                if count > _MAX_COUNT_FILES:
                    return _result(check, None, False, "文件数超过统计上限")
            if "-" in check.expected:
                low, high = (int(item) for item in check.expected.split("-", 1))
                ok = low <= count <= high
            else:
                ok = count == int(check.expected)
            return _result(check, ok, True, f"实际 {count} 个文件")
        if check.kind in {"text_contains", "text_not_contains"}:
            if not path.is_file():
                return _result(check, False, True, "目标不是文件")
            if path.stat().st_size > _MAX_TEXT_BYTES:
                return _result(check, None, False, "文件超过 2 MB，未做文本检查")
            text = path.read_text(encoding="utf-8", errors="replace")
            if "\x00" in text[:1000]:
                return _result(check, None, False, "目标看起来是二进制文件")
            found = check.expected in text
            ok = found if check.kind == "text_contains" else not found
            return _result(check, ok, True, "包含" if found else "不包含")
    except OSError as exc:
        return _result(check, None, False, f"无法核对：{exc}")
    return _result(check, None, False, "未知判据")


def evaluate_machine_checks(lines: list[str]) -> list[dict]:
    """执行全部判据；格式错误的条目也算“未确认/失败”。"""
    results: list[dict] = []
    for line in lines or []:
        check, error = parse_machine_check(line)
        if error or check is None:
            results.append(
                {
                    "check": str(line),
                    "kind": "",
                    "path": "",
                    "expected": "",
                    "ok": False,
                    "confirmed": False,
                    "note": error or "无法解析",
                }
            )
            continue
        results.append(evaluate_machine_check(check))
    return results


def criteria_report(lines: list[str]) -> dict:
    """汇总机器判据，返回 status / text / checks。"""
    checks = evaluate_machine_checks(lines)
    if not checks:
        return {
            "status": "missing",
            "text": "未配置机器判据：这次任务只能标「未确认」，不能自动判定完成。",
            "checks": [],
        }
    failed = [item for item in checks if item["confirmed"] and not item["ok"]]
    unconfirmed = [item for item in checks if not item["confirmed"]]
    if failed:
        status = "failed"
    elif unconfirmed:
        status = "unconfirmed"
    else:
        status = "passed"

    lines_out = []
    for item in checks:
        if not item["confirmed"]:
            mark = "未确认"
        elif item["ok"]:
            mark = "通过"
        else:
            mark = "失败"
        lines_out.append(f"[{mark}] {item['check']}（{item['note']}）")
    summary = {
        "passed": "机器判据全部通过。",
        "failed": "机器判据存在失败项，不能算完成。",
        "unconfirmed": "机器判据有无法核对的项，必须人工确认。",
        "missing": "未配置机器判据，不能自动判定完成。",
    }[status]
    return {"status": status, "text": summary + "\n" + "\n".join(lines_out), "checks": checks}
