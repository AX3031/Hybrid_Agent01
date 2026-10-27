"""基于 OmniPilot 本地工具层的桌面控制工具。

设计取舍：
- 只复用 OmniPilot 的工具实现（screen / gui / clipboard / apps），不引入它自己的
  Agent 循环。这样仍然是单层 Agent：少一次模型往返，每一步都能走本项目的
  HumanInTheLoopMiddleware、操作记录和审计日志。
- 18 个动作收敛成一个 `computer_use` 工具，避免把 OmniPilot 的 41 个工具全部塞给
  模型；动作参数见函数签名，只填当前动作需要的字段。
- 截图以 LangChain content block 的形式回传，qwen3.5-flash 原生支持图片输入，
  可以直接“看见”屏幕。

安全约定：
- 只有 screen_size / locate_image / wait 直接执行；
- 截图、剪贴板、应用列表视为敏感读取，和所有鼠标键盘动作一样，调用前必须由用户确认；
- `app_open` 拒绝启动终端、脚本宿主、注册表/磁盘工具，防止绕过 run_command 的黑名单；
- `app_close` 拒绝关闭系统关键进程；
- 截图和剪贴板内容可能包含隐私，确认卡片与提示词都会提醒用户。
"""

from __future__ import annotations

import base64
import io
import os
import re
import time
from pathlib import Path
from typing import Annotated, Any, Literal

from langchain_core.tools import tool

from agent.tools.safety import guard_path, sanitize_tool_args
from utils.logger_handler import logger
from utils.path_tool import get_abs_path

# OmniPilot 默认把截图/会话写到 ~/.omnipilot；改到项目 logs 下，便于统一清理。
# 必须在导入 omnipilot 之前设置，因为 constants.py 在导入期读取该变量。
os.environ.setdefault("OMNIPILOT_HOME", get_abs_path("logs/omnipilot"))

try:
    from omnipilot.tools.apps import CloseApplication, ListApplications, OpenApplication
    from omnipilot.tools.base import ToolContext
    from omnipilot.tools.clipboard import GetClipboard, SetClipboard
    from omnipilot.tools.gui import (
        Click,
        DoubleClick,
        DragTo,
        Hotkey,
        MoveMouse,
        PressKey,
        RightClick,
        Scroll,
        TypeText,
    )
    from omnipilot.tools.screen import LocateOnScreen, Screenshot, ScreenSize

    # 防御性覆盖：即使 constants 在别处先被导入，也保证截图落在项目 logs 下。
    from omnipilot.tools import screen as _screen_module

    _screen_module.SCREENSHOTS_DIR = Path(get_abs_path("logs/omnipilot/screenshots"))

    _OMNIPILOT_IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover - 只在依赖缺失/损坏时触发
    _OMNIPILOT_IMPORT_ERROR = (
        f"OmniPilot 未安装或加载失败：{exc}。"
        "请执行 uv sync（依赖 omnipilot-agent[gui]）后重启应用。"
    )


ComputerAction = Literal[
    "screenshot",
    "screen_size",
    "locate_image",
    "wait",
    "click",
    "double_click",
    "right_click",
    "move_mouse",
    "drag",
    "scroll",
    "type_text",
    "press_key",
    "hotkey",
    "clipboard_get",
    "clipboard_set",
    "app_open",
    "app_close",
    "app_list",
]

# 只读、低敏感动作：不弹确认卡片。截图/剪贴板/应用列表都可能暴露隐私，因此仍需确认。
READ_ONLY_ACTIONS = frozenset({"screen_size", "locate_image", "wait"})

# 每个动作真正会传给 OmniPilot 的参数，其余参数一律忽略，避免模型串参数。
_ACTION_PARAMS: dict[str, tuple[str, ...]] = {
    "screenshot": ("region", "attach"),
    "screen_size": (),
    "locate_image": ("image_path", "confidence"),
    "wait": ("seconds",),
    "click": ("x", "y", "button", "clicks"),
    "double_click": ("x", "y"),
    "right_click": ("x", "y"),
    "move_mouse": ("x", "y"),
    "drag": ("x1", "y1", "x2", "y2", "button"),
    "scroll": ("amount", "x", "y"),
    "type_text": ("text", "interval"),
    "press_key": ("key", "presses"),
    "hotkey": ("keys",),
    "clipboard_get": (),
    "clipboard_set": ("text",),
    "app_open": ("name", "arguments"),
    "app_close": ("name", "force"),
    "app_list": ("filter", "limit"),
}

_REQUIRED_PARAMS: dict[str, tuple[str, ...]] = {
    "locate_image": ("image_path",),
    "click": ("x", "y"),
    "double_click": ("x", "y"),
    "right_click": ("x", "y"),
    "move_mouse": ("x", "y"),
    "drag": ("x1", "y1", "x2", "y2"),
    "scroll": ("amount",),
    "type_text": ("text",),
    "press_key": ("key",),
    "hotkey": ("keys",),
    "clipboard_set": ("text",),
    "app_open": ("name",),
    "app_close": ("name",),
}

# 允许启动的“友好名称”以外的危险程序：终端/脚本宿主可以绕过 run_command 黑名单；
# 注册表/磁盘/策略工具误触代价太高，统一在入口拒绝。
_BLOCKED_APPS = {
    "cmd",
    "cmd.exe",
    "powershell",
    "powershell.exe",
    "powershell_ise",
    "powershell_ise.exe",
    "pwsh",
    "pwsh.exe",
    "wt",
    "wt.exe",
    "windowsterminal",
    "windowsterminal.exe",
    "conhost",
    "conhost.exe",
    "openconsole",
    "openconsole.exe",
    "bash",
    "bash.exe",
    "wsl",
    "wsl.exe",
    "cscript",
    "cscript.exe",
    "wscript",
    "wscript.exe",
    "mshta",
    "mshta.exe",
    "rundll32",
    "rundll32.exe",
    "regsvr32",
    "regsvr32.exe",
    "regedit",
    "regedit.exe",
    "regedt32",
    "regedt32.exe",
    "diskpart",
    "diskpart.exe",
    "bcdedit",
    "bcdedit.exe",
    "gpedit.msc",
    "secpol.msc",
    "certlm.msc",
    "format.com",
}

# 关闭这些进程会直接破坏桌面/系统会话，即使 force=True 也拒绝。
_PROTECTED_PROCESSES = {
    "system",
    "registry",
    "smss.exe",
    "csrss.exe",
    "wininit.exe",
    "services.exe",
    "lsass.exe",
    "winlogon.exe",
    "svchost.exe",
    "explorer.exe",
    "dwm.exe",
    "fontdrvhost.exe",
    "spoolsv.exe",
    "audiodg.exe",
    "sihost.exe",
    "ctfmon.exe",
    "taskhostw.exe",
}

_PROTECTED_STEMS = {item.rsplit(".", 1)[0] for item in _PROTECTED_PROCESSES}

# 名称/参数里出现这些字符时，Windows 的 `cmd /c start` 可能把它们解释成命令分隔符。
_BLOCKED_APP_CHARS = set('&|<>^%()";\r\n')

# OmniPilot 在 Windows 上用 `cmd /c start` 启动友好名称；这些关键词覆盖常见的终端/脚本宿主变体。
_BLOCKED_APP_KEYWORDS = (
    "powershell",
    "pwsh",
    "windows terminal",
    "windowsterminal",
    "terminal",
    "iterm",
    "iterm2",
    "alacritty",
    "kitty",
    "wezterm",
    "cmder",
    "conemu",
    "mintty",
    "cygwin",
    "msys2",
    "gnome-terminal",
    "konsole",
    "xterm",
    "xfce4-terminal",
    "wsl",
    "bash",
    "conhost",
    "openconsole",
    "cscript",
    "wscript",
    "mshta",
    "rundll32",
    "regsvr32",
    "regedit",
    "diskpart",
    "bcdedit",
    "gpedit",
    "secpol",
    "certlm",
    "format.com",
)

_MAX_TEXT_CHARS = 10_000
_MAX_CLIPBOARD_CHARS = 20_000
_MAX_WAIT_SECONDS = 10.0
# 原图不超过这个像素数时直接发送原图，保证图片像素与屏幕坐标一致；
# 4K（约 830 万像素）会走缩放回退，并在结果里给出换算比例。
_MAX_FULL_IMAGE_PIXELS = 6_000_000


def action_requires_confirmation(action: Any) -> bool:
    """判断某个动作是否需要人工确认；未知/缺失动作按“需要确认”处理（fail closed）。"""
    return str(action or "").strip().lower() not in READ_ONLY_ACTIONS


def _fail(message: str) -> str:
    logger.warning(f"[computer_use]{message}")
    return f"桌面控制失败：{message}"


def _validate_action(action: str) -> str:
    if action not in _ACTION_PARAMS:
        return f"未知动作 {action!r}。可用动作：{', '.join(_ACTION_PARAMS)}"
    return ""


def _validate_required(action: str, args: dict[str, Any]) -> str:
    missing = [name for name in _REQUIRED_PARAMS.get(action, ()) if args.get(name) is None]
    if missing:
        return f"动作 {action} 缺少参数：{', '.join(missing)}"
    return ""


def _as_int(value: Any, name: str) -> tuple[int | None, str]:
    if value is None:
        return None, ""
    try:
        return int(value), ""
    except (TypeError, ValueError):
        return None, f"参数 {name} 必须是整数，收到：{value!r}"


def _as_float(value: Any, name: str) -> tuple[float | None, str]:
    if value is None:
        return None, ""
    try:
        return float(value), ""
    except (TypeError, ValueError):
        return None, f"参数 {name} 必须是数字，收到：{value!r}"


def _as_bool(value: Any, default: bool = False) -> bool:
    """兼容模型偶尔把布尔值写成字符串的情况（'false' 不能被 bool() 当成 True）。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _normalize_keys(value: Any) -> list[str] | None:
    if isinstance(value, str):
        return [part for part in re.split(r"[+\s]+", value.strip()) if part]
    if isinstance(value, (list, tuple)):
        return [str(part).strip() for part in value if str(part).strip()]
    return None


def _guard_locate_image(raw: str) -> tuple[str, str]:
    target, error = guard_path(raw)
    if error:
        return "", error
    assert target is not None
    if not target.is_file():
        return "", f"模板图片不存在：{target}"
    return str(target), ""


def _guard_app_open(name: str) -> tuple[str, str]:
    """拦截终端/脚本宿主/系统工具；路径型名称必须落在工作区域内。"""
    text = str(name).strip()
    if not text:
        return "", "应用名不能为空"

    base = Path(text).name.lower()
    stem = base.rsplit(".", 1)[0] if "." in base else base
    if base in _BLOCKED_APPS or stem in _BLOCKED_APPS:
        return "", (
            f"出于安全考虑，拒绝启动高风险程序：{text}。"
            "如果确实需要执行命令，请使用 run_command（带确认与危险命令黑名单）。"
        )
    if any(keyword in base or keyword in stem for keyword in _BLOCKED_APP_KEYWORDS):
        return "", (
            f"出于安全考虑，拒绝启动终端/脚本宿主：{text}。"
            "如果确实需要执行命令，请使用 run_command。"
        )
    if any(ch in text for ch in _BLOCKED_APP_CHARS):
        return "", f"应用名包含不允许的特殊字符，已拒绝：{text}"

    path_like = (
        os.path.isabs(text)
        or "/" in text
        or "\\" in text
        or os.path.exists(text)
    )
    if path_like:
        target, error = guard_path(text)
        if error:
            return "", error
        assert target is not None
        if not target.exists():
            return "", f"程序不存在：{target}"
        return str(target), ""
    return text, ""


def _guard_app_close(name: str) -> tuple[str, str]:
    text = str(name).strip()
    if not text:
        return "", "应用名不能为空"
    base = Path(text).name.lower()
    stem = base.rsplit(".", 1)[0] if "." in base else base
    if base in _PROTECTED_PROCESSES or stem in _PROTECTED_STEMS:
        return "", f"出于安全考虑，拒绝关闭系统关键进程：{text}"
    # CloseApplication 是按子串/前缀匹配进程名的；这里同样拦住 "expl"、"svch" 这类缩写。
    if len(stem) >= 4 and any(protected.startswith(stem) for protected in _PROTECTED_STEMS):
        return "", f"出于安全考虑，拒绝关闭系统关键进程：{text}"
    if any(protected in stem for protected in _PROTECTED_STEMS if len(protected) >= 4):
        return "", f"出于安全考虑，拒绝关闭系统关键进程：{text}"
    return text, ""


def _prepare_arguments(action: str, args: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """校验并裁剪参数，返回可直接传给 OmniPilot 的 dict。"""
    if error := _validate_action(action):
        return {}, error
    if error := _validate_required(action, args):
        return {}, error

    prepared = {name: args.get(name) for name in _ACTION_PARAMS[action] if args.get(name) is not None}

    if action == "screenshot":
        region = prepared.get("region")
        if region is not None:
            if not isinstance(region, (list, tuple)) or len(region) != 4:
                return {}, "参数 region 必须是 [x, y, width, height] 四个整数"
            try:
                region = [int(value) for value in region]
            except (TypeError, ValueError):
                return {}, "参数 region 必须是 [x, y, width, height] 四个整数"
            if region[2] <= 0 or region[3] <= 0:
                return {}, "参数 region 的 width/height 必须大于 0"
            prepared["region"] = region
        prepared["attach"] = _as_bool(prepared.get("attach", True), default=True)

    elif action == "locate_image":
        image_path, error = _guard_locate_image(str(prepared["image_path"]))
        if error:
            return {}, error
        prepared["image_path"] = image_path
        confidence, error = _as_float(prepared.get("confidence"), "confidence")
        if error:
            return {}, error
        if confidence is not None:
            if not 0 < confidence <= 1:
                return {}, "参数 confidence 必须在 0 到 1 之间"
            prepared["confidence"] = confidence
        else:
            # 不传 confidence 时 pyautogui 用精确匹配，不需要 opencv-python。
            prepared.pop("confidence", None)

    elif action == "wait":
        seconds, error = _as_float(prepared.get("seconds", 1.0), "seconds")
        if error:
            return {}, error
        seconds = 1.0 if seconds is None else seconds
        if seconds < 0 or seconds > _MAX_WAIT_SECONDS:
            return {}, f"参数 seconds 必须在 0 到 {_MAX_WAIT_SECONDS:g} 之间"
        prepared["seconds"] = seconds

    elif action in {"click", "double_click", "right_click", "move_mouse"}:
        for name in ("x", "y"):
            value, error = _as_int(prepared.get(name), name)
            if error:
                return {}, error
            if value is None or value < 0:
                return {}, f"参数 {name} 必须是大于等于 0 的整数"
            prepared[name] = value
        if action == "click":
            clicks, error = _as_int(prepared.get("clicks", 1), "clicks")
            if error:
                return {}, error
            prepared["clicks"] = min(max(clicks or 1, 1), 3)
            button = str(prepared.get("button", "left")).lower()
            if button not in {"left", "right", "middle"}:
                return {}, "参数 button 只能是 left / right / middle"
            prepared["button"] = button
        else:
            prepared.pop("button", None)
            prepared.pop("clicks", None)

    elif action == "drag":
        for name in ("x1", "y1", "x2", "y2"):
            value, error = _as_int(prepared.get(name), name)
            if error:
                return {}, error
            if value is None or value < 0:
                return {}, f"参数 {name} 必须是大于等于 0 的整数"
            prepared[name] = value
        button = str(prepared.get("button", "left")).lower()
        if button not in {"left", "right", "middle"}:
            return {}, "参数 button 只能是 left / right / middle"
        prepared["button"] = button

    elif action == "scroll":
        amount, error = _as_int(prepared.get("amount"), "amount")
        if error:
            return {}, error
        if amount is None or not -50 <= amount <= 50:
            return {}, "参数 amount 必须是 -50 到 50 之间的整数"
        prepared["amount"] = amount
        for name in ("x", "y"):
            if prepared.get(name) is None:
                prepared.pop(name, None)
                continue
            value, error = _as_int(prepared.get(name), name)
            if error:
                return {}, error
            if value is None or value < 0:
                return {}, f"参数 {name} 必须是大于等于 0 的整数"
            prepared[name] = value

    elif action == "type_text":
        text = str(prepared.get("text", ""))
        if not text:
            return {}, "参数 text 不能为空"
        if len(text) > _MAX_TEXT_CHARS:
            return {}, f"文本过长（{len(text)} 字符），请拆成多次输入，单次不超过 {_MAX_TEXT_CHARS} 字符"
        interval, error = _as_float(prepared.get("interval", 0.0), "interval")
        if error:
            return {}, error
        prepared["text"] = text
        prepared["interval"] = min(max(interval or 0.0, 0.0), 1.0)

    elif action == "press_key":
        key = str(prepared.get("key", "")).strip()
        if not key:
            return {}, "参数 key 不能为空"
        presses, error = _as_int(prepared.get("presses", 1), "presses")
        if error:
            return {}, error
        prepared["key"] = key
        prepared["presses"] = min(max(presses or 1, 1), 20)

    elif action == "hotkey":
        keys = _normalize_keys(prepared.get("keys"))
        if not keys:
            return {}, "参数 keys 不能为空，例如 ['ctrl', 's']"
        if len(keys) > 5:
            return {}, "组合键最多 5 个键"
        prepared["keys"] = keys

    elif action == "clipboard_set":
        text = str(prepared.get("text", ""))
        if len(text) > _MAX_CLIPBOARD_CHARS:
            return {}, f"剪贴板文本过长（{len(text)} 字符），请拆成多次写入"
        prepared["text"] = text

    elif action == "app_open":
        name, error = _guard_app_open(str(prepared.get("name", "")))
        if error:
            return {}, error
        arguments = prepared.get("arguments")
        if isinstance(arguments, str):
            arguments = [arguments] if arguments.strip() else []
        elif arguments is None:
            arguments = []
        else:
            arguments = [str(item) for item in arguments]
        for argument in arguments:
            if any(ch in argument for ch in _BLOCKED_APP_CHARS):
                return {}, f"启动参数包含不允许的特殊字符，已拒绝：{argument}"
        prepared["name"] = name
        prepared["arguments"] = arguments

    elif action == "app_close":
        name, error = _guard_app_close(str(prepared.get("name", "")))
        if error:
            return {}, error
        prepared["name"] = name
        prepared["force"] = _as_bool(prepared.get("force", False))

    elif action == "app_list":
        limit, error = _as_int(prepared.get("limit", 50), "limit")
        if error:
            return {}, error
        prepared["limit"] = min(max(limit or 50, 1), 200)
        if args.get("name_filter") is not None:
            # 函数参数名是 name_filter，OmniPilot 工具参数名是 filter
            prepared["filter"] = str(args["name_filter"])

    return prepared, ""


def _tool_instance(action: str) -> Any:
    """按动作惰性实例化 OmniPilot 工具。"""
    instances = {
        "screenshot": Screenshot,
        "screen_size": ScreenSize,
        "locate_image": LocateOnScreen,
        "click": Click,
        "double_click": DoubleClick,
        "right_click": RightClick,
        "move_mouse": MoveMouse,
        "drag": DragTo,
        "scroll": Scroll,
        "type_text": TypeText,
        "press_key": PressKey,
        "hotkey": Hotkey,
        "clipboard_get": GetClipboard,
        "clipboard_set": SetClipboard,
        "app_open": OpenApplication,
        "app_close": CloseApplication,
        "app_list": ListApplications,
    }
    tool_class = instances.get(action)
    return tool_class() if tool_class else None


def _screenshot_image_uri(result: Any) -> tuple[str, str]:
    """返回 (data_uri, 尺寸提示)。

    OmniPilot 默认把截图缩到最长边 1568px；如果直接把缩放图发给模型，模型按图片
    像素估出来的坐标会和真实屏幕坐标不一致。这里优先读 OmniPilot 落盘的原图，
    让图片像素与屏幕一一对应；超大屏（如 4K）才回退到缩放图，并明确给出比例。
    """
    data = result.data or {}
    width = int(data.get("width") or 0)
    height = int(data.get("height") or 0)
    path = data.get("path")

    if path and width and height and width * height <= _MAX_FULL_IMAGE_PIXELS:
        try:
            raw = Path(path).read_bytes()
            encoded = base64.b64encode(raw).decode("ascii")
            return (
                f"data:image/png;base64,{encoded}",
                f"图片为原始分辨率 {width}x{height}，与屏幕坐标一致，可直接按图片像素估坐标。",
            )
        except OSError:
            pass

    uri = result.image.data_uri()
    try:
        from PIL import Image

        with Image.open(io.BytesIO(result.image.raw_bytes())) as image:
            scaled_width, scaled_height = image.size
        if width and scaled_width:
            scale = width / scaled_width
            hint = (
                f"图片已缩小到 {scaled_width}x{scaled_height}，屏幕实际为 {width}x{height}；"
                f"点击坐标 = 图片坐标 × {scale:.4f}。"
            )
        else:
            hint = f"图片已缩小到 {scaled_width}x{scaled_height}，点击前请用 screen_size 核对分辨率。"
    except Exception:
        hint = "图片可能已缩小；点击前请用 screen_size 核对分辨率。"
    return uri, hint


def _format_result(action: str, result: Any) -> str | list[dict]:
    """把 OmniPilot 的 ToolResult 转成 LangChain 可接受的工具输出。"""
    text = result.message_for_model()
    if not result.success:
        return _fail(result.error or text or "未知错误")

    if action == "screenshot" and result.image is not None:
        uri, size_hint = _screenshot_image_uri(result)
        data = result.data if isinstance(result.data, dict) else {}
        screenshot_path = str(data.get("path") or "").strip()
        path_note = f"\n截图文件：{screenshot_path}" if screenshot_path else ""
        return [
            {
                "type": "text",
                "text": (
                    f"{text}\n\n（{size_hint} 坐标原点在左上角，单位为像素。）"
                    f"{path_note}"
                ),
            },
            {
                "type": "image_url",
                "image_url": {"url": uri},
            },
        ]
    if action == "clipboard_get" and len(text) > _MAX_CLIPBOARD_CHARS:
        return text[:_MAX_CLIPBOARD_CHARS] + f"…（已截断，共 {len(text)} 字符）"
    return text or "(no output)"


@tool(description=(
    "控制本机真实桌面（基于 OmniPilot 本地工具层，单工具多动作）。"
    "动作分四组："
    "屏幕：screenshot（截图并附加图片）、screen_size（分辨率）、locate_image（按模板找图）、wait（等待）；"
    "鼠标：click、double_click、right_click、move_mouse、drag、scroll；"
    "键盘：type_text、press_key、hotkey；"
    "系统：clipboard_get、clipboard_set、app_open、app_close、app_list。"
    "坐标原点在屏幕左上角，单位为像素；做点击/输入前先 screenshot 或 screen_size 确认位置。"
    "只有 screen_size、locate_image、wait 直接执行；截图、剪贴板、应用列表和所有鼠标键盘动作"
    "都会先请求用户确认。"
))
def computer_use(
    action: Annotated[ComputerAction, "要执行的动作，见工具描述中的动作分组。"],
    x: Annotated[int | None, "鼠标 x 坐标（像素）。click / double_click / right_click / move_mouse / scroll 使用。"] = None,
    y: Annotated[int | None, "鼠标 y 坐标（像素）。click / double_click / right_click / move_mouse / scroll 使用。"] = None,
    x1: Annotated[int | None, "拖拽起点 x 坐标。drag 使用。"] = None,
    y1: Annotated[int | None, "拖拽起点 y 坐标。drag 使用。"] = None,
    x2: Annotated[int | None, "拖拽终点 x 坐标。drag 使用。"] = None,
    y2: Annotated[int | None, "拖拽终点 y 坐标。drag 使用。"] = None,
    button: Annotated[str, "鼠标按键：left / right / middle。click / drag 使用，默认 left。"] = "left",
    clicks: Annotated[int, "点击次数，1-3。click 使用，默认 1。"] = 1,
    amount: Annotated[int | None, "滚轮格数，正数向上、负数向下，-50 到 50。scroll 使用。"] = None,
    text: Annotated[str | None, "要输入或写入剪贴板的文字。type_text / clipboard_set 使用。"] = None,
    interval: Annotated[float, "逐字输入间隔秒数，0 表示用剪贴板粘贴（支持中文）。type_text 使用。"] = 0.0,
    key: Annotated[str | None, "按键名，如 enter、tab、escape、f5、up、down。press_key 使用。"] = None,
    presses: Annotated[int, "按键次数，1-20。press_key 使用，默认 1。"] = 1,
    keys: Annotated[list[str] | str | None, "组合键，如 ['ctrl','s'] 或 'ctrl+s'。hotkey 使用。"] = None,
    name: Annotated[str | None, "应用名称（如 Chrome、Calculator）或可执行文件路径。app_open / app_close 使用。"] = None,
    arguments: Annotated[list[str] | str | None, "启动应用时的命令行参数。app_open 使用。"] = None,
    force: Annotated[bool, "关闭应用时是否强制结束。app_close 使用，默认 false。"] = False,
    name_filter: Annotated[str | None, "按名称过滤运行中的应用。app_list 使用。"] = None,
    limit: Annotated[int, "应用列表返回数量上限，1-200。app_list 使用，默认 50。"] = 50,
    image_path: Annotated[str | None, "要在屏幕上查找的模板 PNG 路径，必须在工作区域内。locate_image 使用。"] = None,
    confidence: Annotated[float | None, "图片匹配阈值 0-1；省略则精确匹配（无需 opencv-python）。locate_image 使用。"] = None,
    region: Annotated[list[int] | None, "截图区域 [x, y, width, height]，省略为全屏。screenshot 使用。"] = None,
    attach: Annotated[bool, "截图是否附加给模型查看，默认 true。screenshot 使用。"] = True,
    seconds: Annotated[float, "等待秒数，0-10，默认 1。wait 使用。"] = 1.0,
) -> str | list[dict]:
    """执行一个桌面控制动作，返回给模型看的文本；截图会额外返回图片内容块。"""
    if _OMNIPILOT_IMPORT_ERROR:
        return _fail(_OMNIPILOT_IMPORT_ERROR)

    action = str(action).strip().lower()
    raw_args = {
        "x": x,
        "y": y,
        "x1": x1,
        "y1": y1,
        "x2": x2,
        "y2": y2,
        "button": button,
        "clicks": clicks,
        "amount": amount,
        "text": text,
        "interval": interval,
        "key": key,
        "presses": presses,
        "keys": keys,
        "name": name,
        "arguments": arguments,
        "force": force,
        "name_filter": name_filter,
        "limit": limit,
        "image_path": image_path,
        "confidence": confidence,
        "region": region,
        "attach": attach,
        "seconds": seconds,
    }
    prepared, error = _prepare_arguments(action, raw_args)
    if error:
        return _fail(error)

    if action == "wait":
        time.sleep(float(prepared["seconds"]))
        return f"已等待 {prepared['seconds']:g} 秒。"

    tool_instance = _tool_instance(action)
    if tool_instance is None:  # pragma: no cover - 由 _validate_action 兜底
        return _fail(f"动作 {action} 没有对应的 OmniPilot 工具")

    logger.info(
        f"[computer_use]执行动作 {action}，参数 "
        f"{sanitize_tool_args('computer_use', prepared)}"
    )
    try:
        result = tool_instance.execute(ToolContext(logger=logger), prepared)
    except Exception as exc:  # pragma: no cover - OmniPilot 内部已捕获大多数异常
        logger.error(f"[computer_use]动作 {action} 异常：{exc}")
        return _fail(f"{type(exc).__name__}: {exc}")
    return _format_result(action, result)


if __name__ == "__main__":
    # 手工冒烟：只测试只读动作，避免误触鼠标键盘。
    print(computer_use.invoke({"action": "screen_size"}))
    print(computer_use.invoke({"action": "app_list", "limit": 5}))
    print(computer_use.invoke({"action": "wait", "seconds": 0.1}))
