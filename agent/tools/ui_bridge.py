"""Windows UI Automation / OCR 桥（零依赖）。

为什么不用 Python 的 UIA 库：
- 本机是 Python 3.14，`uiautomation`/`pywinauto`/`comtypes` 都还没有可用的轮子，
  硬装会把现有环境搞坏；
- Windows 自带 `UIAutomationClient`（.NET）与 `Windows.Media.Ocr`（中文语言包
  已在系统里），用 PowerShell 调它们不需要安装任何东西；
- 项目本来就用 PowerShell 解析 `.lnk` 真实目标，这条依赖是既有的。

代价：每次调用要起一个 PowerShell 进程（约 0.3–0.6 秒）。对“点一个按钮”来说
完全可接受，而且比让模型猜坐标便宜得多。

安全边界不在这里做：哪些窗口不能碰、密码框不能读、文本要脱敏，都由
`computer_use.py` 统一判断，桥只负责“把系统真实状态如实返回”。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from utils.logger_handler import logger

# 单次调用上限：查找控件可能扫到很大的树，超过这个时间就当作目标程序卡死。
DEFAULT_TIMEOUT_SECONDS = 12.0
_MAX_OUTPUT_BYTES = 4 * 1024 * 1024


class UiBridgeError(RuntimeError):
    """桥调用失败（未安装/超时/目标不存在/COM 报错）。"""


def bridge_script() -> Path:
    return Path(__file__).with_name("uia_bridge.ps1")


def powershell_path() -> str:
    """返回可用的 PowerShell 可执行文件；没有则返回空字符串。"""
    for name in ("powershell.exe", "powershell", "pwsh.exe", "pwsh"):
        found = shutil.which(name)
        if found:
            return found
    return ""


def available() -> bool:
    """桥能不能用：Windows + 有 PowerShell + 脚本在。"""
    if os.name != "nt":
        return False
    if not bridge_script().is_file():
        return False
    return bool(powershell_path())


def run_bridge(action: str, payload: dict | None = None, timeout: float | None = None) -> dict:
    """调用桥并返回 `data` 字段；失败抛 `UiBridgeError`。"""
    if os.name != "nt":
        raise UiBridgeError("UI 自动化只在 Windows 上可用")
    script = bridge_script()
    if not script.is_file():
        raise UiBridgeError(f"桥脚本缺失：{script}")
    shell = powershell_path()
    if not shell:
        raise UiBridgeError("找不到 PowerShell，无法调用系统 UI 自动化")

    body = {"action": action, **(payload or {})}
    workdir = Path(tempfile.mkdtemp(prefix="agent_uia_"))
    input_file = workdir / "in.json"
    output_file = workdir / "out.json"
    try:
        input_file.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        command = [
            shell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-InputFile",
            str(input_file),
            "-OutputFile",
            str(output_file),
        ]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            completed = subprocess.run(  # noqa: S603 - 参数是固定列表，不经过 shell
                command,
                capture_output=True,
                timeout=timeout or DEFAULT_TIMEOUT_SECONDS,
                creationflags=flags,
            )
        except subprocess.TimeoutExpired as exc:
            raise UiBridgeError(
                f"UI 自动化调用超时（{timeout or DEFAULT_TIMEOUT_SECONDS:g} 秒）：目标程序可能无响应"
            ) from exc

        if not output_file.is_file():
            detail = (completed.stderr or b"").decode("utf-8", "replace").strip()[:500]
            raise UiBridgeError(f"UI 自动化没有返回结果（退出码 {completed.returncode}）：{detail}")
        raw = output_file.read_bytes()
        if len(raw) > _MAX_OUTPUT_BYTES:
            raise UiBridgeError("UI 自动化返回内容过大，已拒绝处理")
        try:
            parsed = json.loads(raw.decode("utf-8", "replace"))
        except ValueError as exc:
            raise UiBridgeError(f"UI 自动化返回的不是合法 JSON：{exc}") from exc
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    if not parsed.get("ok"):
        raise UiBridgeError(str(parsed.get("error") or "未知错误"))
    data = parsed.get("data")
    return data if isinstance(data, dict) else {}


def list_windows(limit: int = 40, filter_text: str = "") -> list[dict]:
    """列出可见顶层窗口。"""
    data = run_bridge("windows", {"limit": limit, "filter": filter_text})
    rows = data.get("windows") or []
    return [row for row in rows if isinstance(row, dict)]


def find_window(*, window: str = "", pid: int | None = None) -> dict:
    """只解析目标窗口信息（不扫控件树，快）；找不到抛 UiBridgeError。"""
    data = run_bridge("window", {"window": window, "pid": pid}, timeout=8.0)
    info = data.get("window")
    return info if isinstance(info, dict) else {}


def dump_tree(
    *,
    window: str = "",
    pid: int | None = None,
    depth: int = 3,
    limit: int = 120,
    filter_text: str = "",
) -> dict:
    """导出一个窗口的控件树（ControlView 层级）。"""
    return run_bridge(
        "tree",
        {
            "window": window,
            "pid": pid,
            "depth": depth,
            "limit": limit,
            "filter": filter_text,
        },
    )


def invoke_element(
    *,
    window: str = "",
    pid: int | None = None,
    name: str = "",
    aid: str = "",
    control_type: str = "",
) -> dict:
    """按名字/AutomationId 触发控件：优先 InvokePattern，否则返回可点坐标。"""
    return run_bridge(
        "invoke",
        {"window": window, "pid": pid, "name": name, "aid": aid, "type": control_type},
    )


def set_element_value(
    *,
    window: str = "",
    pid: int | None = None,
    name: str = "",
    aid: str = "",
    control_type: str = "",
    text: str = "",
) -> dict:
    """写入控件值：优先 ValuePattern，否则返回可点坐标由调用方输入。"""
    return run_bridge(
        "set_value",
        {
            "window": window,
            "pid": pid,
            "name": name,
            "aid": aid,
            "type": control_type,
            "text": text,
        },
    )


def ocr_image(path: str) -> dict:
    """用 Windows 内置 OCR 识别一张本地图片。"""
    # WinRT 的 StorageFile 只接受绝对路径，这里统一解析，调用方不必关心。
    absolute = Path(path).expanduser().resolve()
    if not absolute.is_file():
        raise UiBridgeError(f"图片不存在：{absolute}")
    data = run_bridge("ocr", {"image": str(absolute)})
    lines = data.get("lines") or []
    data["lines"] = [line for line in lines if isinstance(line, dict)]
    logger.info(
        f"[uia]OCR 完成：语言 {data.get('lang', '?')}，识别到 {len(data['lines'])} 行"
    )
    return data
