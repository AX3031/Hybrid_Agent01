"""基于 OmniPilot 本地工具层的桌面控制工具。

设计取舍：
- 只复用 OmniPilot 的工具实现（screen / gui / clipboard / apps），不引入它自己的
  Agent 循环。这样仍然是单层 Agent：少一次模型往返，每一步都能走本项目的
  HumanInTheLoopMiddleware、操作记录和审计日志。
- 26 个动作收敛成一个 `computer_use` 工具，避免把 OmniPilot 的 41 个工具全部塞给
  模型；动作参数见函数签名，只填当前动作需要的字段。其中 7 个控件级/OCR 动作走
  `uia_bridge.ps1`（Windows 自带 UI 自动化 + 系统 OCR，不装新依赖）。
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
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Annotated, Any, Literal

from langchain_core.tools import tool

from agent.tools import ui_bridge
from agent.tools.safety import guard_path, sanitize_tool_args
from agent.tools.ui_bridge import UiBridgeError
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
    "app_open_start_menu",
    "app_close",
    "app_list",
    "ui_windows",
    "ui_tree",
    "ui_wait",
    "ui_click",
    "ui_set_text",
    "ocr_text",
    "click_text",
]

# 只读、低敏感动作：不弹确认卡片。截图/剪贴板/应用列表都可能暴露隐私，因此仍需确认。
# ui_wait 只返回“找到没找到 + 耗时”，不带屏幕内容，所以也不弹确认。
READ_ONLY_ACTIONS = frozenset({"screen_size", "locate_image", "wait", "ui_wait"})

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
    "app_open_start_menu": ("name",),
    "app_close": ("name", "force"),
    "app_list": ("filter", "limit"),
    "ui_windows": ("name_filter", "limit"),
    "ui_tree": ("window", "pid", "depth", "limit", "name_filter"),
    "ui_wait": ("window", "pid", "name", "automation_id", "control_type", "timeout"),
    "ui_click": ("window", "pid", "name", "automation_id", "control_type"),
    "ui_set_text": ("window", "pid", "name", "automation_id", "control_type", "text"),
    "ocr_text": ("region", "window"),
    "click_text": ("text", "index", "window"),
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
    "app_open_start_menu": ("name",),
    "app_close": ("name",),
    "ui_set_text": ("text",),
    "click_text": ("text",),
}

# UI 自动化的输出上限：控件树比截图小，但也不能把整棵浏览器树塞给模型。
_MAX_UI_NODES = 120
_MAX_UI_TREE_CHARS = 6000
_MAX_UI_WINDOWS = 40
_MAX_OCR_LINES = 60
_MAX_UI_WAIT_SECONDS = 30.0

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

# 开始菜单/控制面板里高危程序的显示名（中英文）：`cmd /c start` 与快捷方式启动
# 都可能按这些名字拉起终端/注册表/磁盘工具，必须一并拒绝。
_BLOCKED_APP_DISPLAY_NAMES = (
    "命令提示符",
    "命令提示",
    "注册表编辑器",
    "注册表",
    "任务计划程序",
    "计划任务",
    "磁盘管理",
    "磁盘分区",
    "组件服务",
    "控制面板",
    "服务",
    "本地安全策略",
    "本地组策略",
    "组策略",
    "计算机管理",
    "管理工具",
    "运行",
    "command prompt",
    "commandprompt",
    "registry editor",
    "task scheduler",
    "taskschd",
    "disk management",
    "diskmgmt",
    "component services",
    "comexp",
    "control panel",
    "services.msc",
    "computer management",
    "microsoft management console",
    "group policy",
    "local security",
    "run dialog",
)

_MAX_TEXT_CHARS = 10_000
_MAX_CLIPBOARD_CHARS = 20_000
_MAX_WAIT_SECONDS = 10.0
# 原图不超过这个像素数时直接发送原图，保证图片像素与屏幕坐标一致；
# 4K（约 830 万像素）会走缩放回退，并在结果里给出换算比例。
_MAX_FULL_IMAGE_PIXELS = 6_000_000

# 开始菜单兜底用的友好名别名表：微信/QQ 这类国内软件的快捷方式常有中英文两种名字。
_APP_NAME_ALIASES: dict[str, tuple[str, ...]] = {
    "微信": ("wechat", "weixin"),
    "weixin": ("微信", "wechat"),
    "wechat": ("微信", "weixin"),
    "qq": ("腾讯qq", "qqnt"),
    "腾讯qq": ("qq", "qqnt"),
    "钉钉": ("dingtalk",),
    "dingtalk": ("钉钉",),
    "企业微信": ("wxwork", "weixinwork"),
    "wxwork": ("企业微信",),
    "飞书": ("lark",),
    "lark": ("飞书",),
    "计算器": ("calculator",),
    "calculator": ("计算器",),
    "记事本": ("notepad",),
    "notepad": ("记事本",),
    "画图": ("mspaint", "paint"),
    "微软 Edge": ("msedge", "edge"),
    "edge": ("msedge", "microsoft edge"),
    "chrome": ("谷歌浏览器", "google chrome"),
    "谷歌浏览器": ("chrome", "google chrome"),
    "word": ("microsoft word",),
    "excel": ("microsoft excel",),
    "powerpoint": ("microsoft powerpoint",),
}


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


def _coerce_str_list(value: Any, *, separators: str = r"[+\s]+") -> list[str] | None:
    """把“本该是列表但模型写成了字符串”的参数还原成字符串列表。

    模型经常把数组参数序列化成字符串，例如 hotkey 的 keys 收到的是
    `'["alt", "tab"]'` 而不是 `["alt", "tab"]`。以前按分隔符切这种字符串会切出
    `'["alt",'`、`'"tab"]'` 这样的垃圾键名：热键“执行成功”但实际什么都没按到。
    这里先认出 JSON 数组并解析，解析不了再退回按分隔符切，并清掉残留的引号/方括号。
    """
    if isinstance(value, (list, tuple)):
        items: list[str] = []
        for part in value:
            # 元素本身又被序列化一次时（['["alt", "tab"]']）拆开后并入
            items.extend(_coerce_str_list(part, separators=separators) or [])
        return [item for item in (part.strip() for part in items) if item]
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, (list, tuple)):
            return [item for item in (str(part).strip() for part in parsed) if item]
        # 不是合法 JSON 时按垃圾字符串处理：去掉包裹的方括号再切
        text = text[1:-1]
    cleaned: list[str] = []
    for part in re.split(separators, text.strip("[]")):
        token = part.strip().strip(",").strip()
        if len(token) >= 2 and token[0] in "'\"" and token[-1] == token[0]:
            token = token[1:-1]
        if token:
            cleaned.append(token)
    return cleaned


def _normalize_keys(value: Any) -> list[str] | None:
    return _coerce_str_list(value)


def _guard_locate_image(raw: str) -> tuple[str, str]:
    target, error = guard_path(raw)
    if error:
        return "", error
    assert target is not None
    if not target.is_file():
        return "", f"模板图片不存在：{target}"
    return str(target), ""


def _is_high_risk_display_name(name: str) -> bool:
    """按中英文显示名精确判断是否指向终端/注册表/磁盘/管理单元等高危程序。

    用精确匹配而不是子串，避免名字里含“服务/运行”等常用词的正常软件被误拦；
    同时兼容快捷方式名（去 `.lnk`）与目标 exe 名（去 `.exe`）。
    """
    text = str(name or "").strip().lower()
    if not text:
        return False
    base = Path(text).name
    stem = base[: -len(".lnk")] if base.endswith(".lnk") else base
    candidates = {text, base, stem}
    candidates |= {c[: -len(".exe")] for c in candidates if c.endswith(".exe")}
    return bool(candidates & set(_BLOCKED_APP_DISPLAY_NAMES)) or bool(
        candidates & _BLOCKED_APPS
    )


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
    if _is_high_risk_display_name(text):
        return "", (
            f"出于安全考虑，拒绝启动系统工具（终端/注册表/磁盘/管理单元等）：{text}。"
            "需要执行命令时请使用 run_command（带确认与危险命令黑名单）。"
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


def _app_name_variants(name: str) -> dict[str, str]:
    """生成开始菜单搜索用的候选名 -> 来源（name/alias）。

    中文软件常以英文名命名快捷方式（微信 -> WeChat.lnk），因此把中英别名一并
    纳入候选；别名只用于匹配快捷方式文件名，避免误命无关目标。
    """
    base = str(name or "").strip().lower()
    base = base[: -len(".lnk")] if base.endswith(".lnk") else base
    variants: dict[str, str] = {}
    if base:
        variants[base] = "name"
    for alias in _APP_NAME_ALIASES.get(base, ()):
        key = str(alias).strip().lower()
        if key and key not in variants:
            variants[key] = "alias"
    return variants


def _start_menu_dirs() -> list[Path]:
    """Windows 开始菜单程序目录（本机用户 + 全体用户）。"""
    candidates = [
        Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
        Path(
            os.environ.get(
                "ProgramData",
                r"C:\ProgramData",
            )
        )
        / "Microsoft/Windows/Start Menu/Programs",
    ]
    return [path for path in candidates if path.is_dir()]


def _find_start_menu_shortcut(name: str) -> tuple[Path | None, str]:
    """在开始菜单程序目录里找名字（或别名）命中的快捷方式。

    优先文件名直接命中（返回命中名），其次别名命中（返回空命中名）。
    """
    variants = _app_name_variants(name)
    if not variants:
        return None, ""
    fallback: Path | None = None
    for directory in _start_menu_dirs():
        try:
            for current_root, _dirs, file_names in os.walk(directory):
                for file_name in file_names:
                    if not file_name.lower().endswith(".lnk"):
                        continue
                    stem = file_name[: -len(".lnk")].lower()
                    if stem not in variants:
                        continue
                    path = Path(current_root) / file_name
                    if variants[stem] == "name":
                        return path, stem
                    if fallback is None:
                        fallback = path
        except OSError:
            continue
    if fallback is not None:
        return fallback, ""
    return None, ""


def _list_uwp_apps() -> list[tuple[str, str]]:
    """用 PowerShell 列出已安装的 UWP/商店应用（显示名, AUMID）；失败返回空。"""
    try:
        completed = subprocess.run(  # noqa: S603
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Get-StartApps | ForEach-Object { \"$($_.Name)`t$($_.AppID)\" }",
            ],
            capture_output=True,
            text=True,
            timeout=8,
            encoding="utf-8",
            errors="ignore",
        )
    except (OSError, subprocess.SubprocessError):
        return []
    apps: list[tuple[str, str]] = []
    for line in (completed.stdout or "").splitlines():
        if "\t" not in line:
            continue
        display, _, app_id = line.partition("\t")
        display, app_id = display.strip(), app_id.strip()
        if display and app_id:
            apps.append((display, app_id))
    return apps


def _find_uwp_app(name: str) -> tuple[str, str] | None:
    """在开始应用列表里按显示名/别名匹配一个 UWP 应用，返回（显示名, AUMID）。"""
    variants = _app_name_variants(name)
    if not variants:
        return None
    apps = _list_uwp_apps()
    for display, app_id in apps:
        if display.lower() in variants:
            return display, app_id
    for display, app_id in apps:
        if any(variant in display.lower() for variant in variants):
            return display, app_id
    return None


def _launch_uwp(app_id: str) -> tuple[bool, str]:
    r"""用 `explorer.exe shell:AppsFolder\<AUMID>` 启动 UWP 应用。"""
    try:
        subprocess.Popen(  # noqa: S603
            ["explorer.exe", f"shell:AppsFolder\\{app_id}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True, ""
    except OSError as exc:
        return False, str(exc)


def _is_blocked_program_name(name: str) -> bool:
    """综合判断一个程序名/路径是否命中高危黑名单（精确显示名 + 关键词 + 基名）。"""
    if _is_high_risk_display_name(name):
        return True
    text = str(name or "").strip().lower()
    if not text:
        return False
    base = Path(text).name
    stem = base[: -len(".lnk")] if base.endswith(".lnk") else base
    return any(keyword in base or keyword in stem for keyword in _BLOCKED_APP_KEYWORDS)


def _resolve_shortcut_target(shortcut: Path) -> str:
    """用 PowerShell 解析 .lnk 快捷方式的真实目标路径；失败返回空串。"""
    escaped = str(shortcut).replace("'", "''")
    script = f"(New-Object -ComObject WScript.Shell).CreateShortcut('{escaped}').TargetPath"
    try:
        completed = subprocess.run(  # noqa: S603
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=8,
            encoding="utf-8",
            errors="ignore",
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (completed.stdout or "").strip()


def _launch_start_menu_app(name: str) -> str:
    """Windows 兜底：从开始菜单（快捷方式 / UWP）启动应用，返回给模型看的说明。

    不依赖 `cmd /c start` 的模糊解析，而是把名字归一后到开始菜单程序目录
    与商店应用列表里匹配；命中后还要解析快捷方式真实目标并重新过一遍高危
    黑名单（命令提示符/注册表编辑器/磁盘工具等），避免用中文/显示名绕过。
    """
    if not _start_menu_dirs():
        return _fail("当前系统没有可用的开始菜单目录，无法从开始菜单启动。")
    shortcut, matched = _find_start_menu_shortcut(name)
    if shortcut is not None:
        # fail closed：快捷方式名、目标 exe 名任一命中高危黑名单都拒绝启动。
        target = _resolve_shortcut_target(shortcut)
        blockers = [shortcut.name]
        if target:
            blockers.append(Path(target).name)
        if any(_is_blocked_program_name(item) for item in blockers):
            shown = target or str(shortcut)
            return _fail(
                f"开始菜单命中的程序属于终端/脚本宿主/注册表/磁盘等高危工具，已拒绝启动：{shown}"
            )
        try:
            os.startfile(str(shortcut))  # noqa: S606
        except OSError as exc:
            return _fail(f"从开始菜单启动快捷方式失败：{exc}")
        note = f"（按别名 {matched} 命中）" if matched else ""
        return (
            f"已从 Windows 开始菜单启动「{name}」{note}：{shortcut}\n"
            "启动方法已记录：若本次成功，可提示用户“保存为技能”，下次直接走开始菜单。"
        )
    uwp = _find_uwp_app(name)
    if uwp is not None:
        display, app_id = uwp
        if _is_blocked_program_name(display) or _is_blocked_program_name(app_id):
            return _fail(
                f"开始菜单命中的商店应用属于终端/脚本宿主/系统工具，已拒绝启动：{display}"
            )
        ok, error = _launch_uwp(app_id)
        if ok:
            return (
                f"已从 Windows 开始菜单启动商店应用「{display}」（{app_id}）。\n"
                "启动方法已记录：若本次成功，可提示用户“保存为技能”。"
            )
        return _fail(f"启动商店应用失败：{error}")
    return (
        f"开始菜单里没找到「{name}」的快捷方式或商店应用。\n"
        "建议：先 app_list 确认是否已运行；或换个中文名/英文名重试；"
        "仍不行就告诉用户自行从开始菜单搜索，并确认软件是否已安装。"
    )


_UI_ACTIONS = {
    "ui_windows",
    "ui_tree",
    "ui_wait",
    "ui_click",
    "ui_set_text",
    "ocr_text",
    "click_text",
}


# 本项目的确认卡片在浏览器里的 Streamlit 页面上渲染。如果允许模型自动操作那个页面，
# 等于用 UI 自动化去点“我确认，执行”按钮，绕过人工审批链；因此按标题关键字拒绝。
_UI_SELF_UI_MARKERS = ("streamlit",)


def _is_self_ui_window(title: str) -> bool:
    lowered = str(title or "").lower()
    return any(marker in lowered for marker in _UI_SELF_UI_MARKERS)


def _guard_ui_target(info: dict | None) -> str:
    """判断一个窗口能不能被自动操作；返回空字符串表示可以。

    这里挡的是“绕路”风险：已经开着的终端/注册表/任务管理器可以接受键盘输入，
    相当于把 run_command 的黑名单绕过；不能操作自己（确认按钮在同一个应用里）；
    最小化/不可见窗口的坐标没有意义。
    """
    if not isinstance(info, dict) or not info:
        return "没有拿到目标窗口信息，已拒绝（无法确认操作对象）"
    try:
        pid = int(info.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    title = str(info.get("title") or "").strip()
    proc = str(info.get("proc") or "").strip()
    try:
        width = int(info.get("w") or 0)
        height = int(info.get("h") or 0)
    except (TypeError, ValueError):
        width = height = 0

    if pid and pid == os.getpid():
        return "目标是助手自己的窗口，已拒绝（避免操作自己的确认按钮）"
    if _is_self_ui_window(title):
        return (
            "目标窗口里开着本助手的确认页面，已拒绝自动操作："
            "确认与 PIN 必须由人在页面上完成，模型不能自己去点那个按钮。"
        )
    if width <= 0 or height <= 0:
        return f"窗口「{title or proc or '?'}」当前不可见（已最小化或尺寸为 0），请先恢复窗口"

    lowered = proc.lower()
    stem = lowered[:-4] if lowered.endswith(".exe") else lowered
    if lowered in _BLOCKED_APPS or stem in _BLOCKED_APPS or f"{stem}.exe" in _BLOCKED_APPS:
        return (
            f"目标窗口属于终端/脚本宿主/系统工具（{proc or '?'}），已拒绝自动操作。"
            "要执行命令请用 run_command（带确认与危险命令黑名单）。"
        )
    if lowered in _PROTECTED_PROCESSES or stem in _PROTECTED_STEMS:
        return f"目标窗口属于系统关键进程（{proc or '?'}），已拒绝自动操作"
    if _is_blocked_program_name(proc) or _is_blocked_program_name(title):
        return f"目标窗口是高危系统工具（{title or proc}），已拒绝自动操作"
    return ""


def _format_ui_windows(rows: list[dict]) -> str:
    lines = [f"可见窗口 {len(rows)} 个（可用 window=标题片段 或 pid= 定位）："]
    for row in rows:
        title = str(row.get("title") or "(无标题)")
        flag = " [前台]" if row.get("foreground") else ""
        lines.append(
            f"- {title}{flag}\n"
            f"  pid={row.get('pid')} proc={row.get('proc') or '?'} class={row.get('cls') or '?'} "
            f"rect=({row.get('x')},{row.get('y')},{row.get('w')},{row.get('h')})"
        )
    return "\n".join(lines)


def _format_ui_tree(data: dict) -> str:
    window = data.get("window") or {}
    nodes = [node for node in (data.get("nodes") or []) if isinstance(node, dict)]
    lines = [
        f"窗口「{window.get('title') or '(无标题)'}」pid={window.get('pid')} "
        f"proc={window.get('proc') or '?'} "
        f"rect=({window.get('x')},{window.get('y')},{window.get('w')},{window.get('h')})，"
        f"控件 {len(nodes)} 个："
    ]
    for node in nodes[:_MAX_UI_NODES]:
        name = str(node.get("name") or "")
        if node.get("password"):
            name = "<密码框，内容已隐藏>"
        elif len(name) > 60:
            name = name[:60] + "…"
        parts = [f"[{node.get('i')}] {node.get('type') or '?'}"]
        if name:
            parts.append(f"\"{name}\"")
        if node.get("aid"):
            parts.append(f"aid={node['aid']}")
        parts.append(f"rect=({node.get('x')},{node.get('y')},{node.get('w')},{node.get('h')})")
        if not node.get("enabled", True):
            parts.append("disabled")
        if node.get("offscreen"):
            parts.append("offscreen")
        value = str(node.get("value") or "")
        if value and not node.get("password"):
            parts.append(f"value={value[:40]!r}")
        lines.append("  " + " ".join(parts))
    text = "\n".join(lines)
    if len(text) > _MAX_UI_TREE_CHARS:
        text = (
            text[:_MAX_UI_TREE_CHARS]
            + f"\n…（已截断，共 {len(text)} 字符；可用 name_filter 或减小 depth/limit 缩小范围）"
        )
    return text


def _map_ocr_lines(
    lines: list[dict], scale: float, offset_x: int, offset_y: int
) -> list[dict]:
    """把 OCR 的图片坐标换成屏幕坐标，并算出每行中心点。"""
    mapped: list[dict] = []
    for line in lines:
        text = str(line.get("text") or "").strip()
        if not text:
            continue
        try:
            x = int(line.get("x") or 0)
            y = int(line.get("y") or 0)
            width = int(line.get("w") or 0)
            height = int(line.get("h") or 0)
        except (TypeError, ValueError):
            continue
        sx = offset_x + round(x * scale)
        sy = offset_y + round(y * scale)
        sw = max(1, round(width * scale))
        sh = max(1, round(height * scale))
        mapped.append(
            {
                "text": text,
                "x": sx,
                "y": sy,
                "w": sw,
                "h": sh,
                "cx": sx + sw // 2,
                "cy": sy + sh // 2,
            }
        )
    return mapped


def _select_ocr_line(lines: list[dict], text: str, index: int = 0) -> tuple[dict | None, str]:
    """按“包含”关系在 OCR 结果里选一行；返回 (行, 错误说明)。"""
    needle = str(text or "").strip().lower()
    if not needle:
        return None, "参数 text 不能为空"
    matches = [line for line in lines if needle in str(line.get("text") or "").lower()]
    if not matches:
        sample = "；".join(str(line.get("text") or "")[:16] for line in lines[:6])
        return None, (
            f"屏幕上没有找到包含「{text}」的文字。"
            + (f"当前识别到的高频文字：{sample}" if sample else "当前画面几乎没有可识别文字")
        )
    position = max(0, int(index or 0))
    if position >= len(matches):
        return None, f"「{text}」匹配到 {len(matches)} 处，index={position} 超出范围（从 0 开始）"
    return matches[position], ""


def _click_at(x: int, y: int) -> str:
    """用 OmniPilot 的点击工具点一个屏幕坐标；返回错误说明（空=成功）。"""
    tool_instance = _tool_instance("click")
    if tool_instance is None:  # pragma: no cover - click 总是可用
        return "点击工具不可用"
    try:
        result = tool_instance.execute(
            ToolContext(logger=logger),
            {"x": int(x), "y": int(y), "clicks": 1, "button": "left"},
        )
    except Exception as exc:  # pragma: no cover - OmniPilot 内部已捕获大多数异常
        return f"点击失败：{exc}"
    if not result.success:
        return result.error or "点击失败"
    return ""


def _type_into_focus(text: str) -> str:
    """把文字输入当前焦点（剪贴板粘贴，支持中文）；返回错误说明。"""
    tool_instance = _tool_instance("type_text")
    if tool_instance is None:  # pragma: no cover
        return "输入工具不可用"
    try:
        result = tool_instance.execute(
            ToolContext(logger=logger), {"text": text, "interval": 0}
        )
    except Exception as exc:  # pragma: no cover
        return f"输入失败：{exc}"
    if not result.success:
        return result.error or "输入失败"
    return ""


def _hotkey(keys: list[str]) -> str:
    tool_instance = _tool_instance("hotkey")
    if tool_instance is None:  # pragma: no cover
        return "组合键工具不可用"
    try:
        result = tool_instance.execute(ToolContext(logger=logger), {"keys": list(keys)})
    except Exception as exc:  # pragma: no cover
        return f"组合键失败：{exc}"
    if not result.success:
        return result.error or "组合键失败"
    return ""


def _ui_target_spec(prepared: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """规范化 window/pid；窗口参数不能太长（会拼进 PowerShell 参数）。"""
    window = str(prepared.get("window") or "").strip()
    if window:
        prepared["window"] = window[:120]
    else:
        prepared.pop("window", None)
    if prepared.get("pid") is not None:
        pid, error = _as_int(prepared.get("pid"), "pid")
        if error:
            return {}, error
        if pid is not None and pid <= 0:
            return {}, "参数 pid 必须是正整数"
        prepared["pid"] = pid
    return prepared, ""


def _ui_element_spec(prepared: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """规范化 name/automation_id/control_type。"""
    name = str(prepared.get("name") or "").strip()
    aid = str(prepared.get("automation_id") or "").strip()
    if not name and not aid:
        return {}, "需要 name（控件名字）或 automation_id 之一来定位控件"
    prepared["name"] = name[:120]
    prepared["automation_id"] = aid[:120]
    prepared["control_type"] = str(prepared.get("control_type") or "").strip()[:40]
    return prepared, ""


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
            # 模型也常把 region 写成 '[0, 0, 800, 600]' 这种字符串，先还原再校验
            if isinstance(region, str):
                region = _coerce_str_list(region, separators=r"[,+\s]+")
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
        arguments = _coerce_str_list(prepared.get("arguments"), separators=r"[\s]+") or []
        for argument in arguments:
            if any(ch in argument for ch in _BLOCKED_APP_CHARS):
                return {}, f"启动参数包含不允许的特殊字符，已拒绝：{argument}"
        prepared["name"] = name
        prepared["arguments"] = arguments

    elif action == "app_open_start_menu":
        name, error = _guard_app_open(str(prepared.get("name", "")))
        if error:
            return {}, error
        prepared["name"] = name

    elif action == "ui_windows":
        limit, error = _as_int(prepared.get("limit", _MAX_UI_WINDOWS), "limit")
        if error:
            return {}, error
        prepared["limit"] = min(max(limit or _MAX_UI_WINDOWS, 1), 100)
        prepared["name_filter"] = str(prepared.get("name_filter") or "").strip()[:60]

    elif action == "ui_tree":
        depth, error = _as_int(prepared.get("depth", 3), "depth")
        if error:
            return {}, error
        limit, error = _as_int(prepared.get("limit", _MAX_UI_NODES), "limit")
        if error:
            return {}, error
        prepared["depth"] = min(max(depth or 3, 1), 6)
        prepared["limit"] = min(max(limit or _MAX_UI_NODES, 1), 300)
        prepared["name_filter"] = str(prepared.get("name_filter") or "").strip()[:60]
        prepared, error = _ui_target_spec(prepared)
        if error:
            return {}, error

    elif action == "ui_wait":
        timeout, error = _as_float(prepared.get("timeout", 10.0), "timeout")
        if error:
            return {}, error
        timeout = 10.0 if timeout is None else timeout
        if timeout < 0 or timeout > _MAX_UI_WAIT_SECONDS:
            return {}, f"参数 timeout 必须在 0 到 {_MAX_UI_WAIT_SECONDS:g} 秒之间"
        prepared["timeout"] = timeout
        prepared, error = _ui_target_spec(prepared)
        if error:
            return {}, error
        # ui_wait 可以只等窗口；给了控件名才等控件
        if prepared.get("name") or prepared.get("automation_id"):
            prepared, error = _ui_element_spec(prepared)
            if error:
                return {}, error

    elif action in {"ui_click", "ui_set_text"}:
        prepared, error = _ui_target_spec(prepared)
        if error:
            return {}, error
        prepared, error = _ui_element_spec(prepared)
        if error:
            return {}, error
        if action == "ui_set_text":
            text = str(prepared.get("text", ""))
            if not text:
                return {}, "参数 text 不能为空"
            if len(text) > _MAX_TEXT_CHARS:
                return {}, f"文本过长（{len(text)} 字符），请拆成多次写入"
            prepared["text"] = text

    elif action == "ocr_text":
        region = prepared.get("region")
        if region is not None:
            if isinstance(region, str):
                region = _coerce_str_list(region, separators=r"[,+\s]+")
            if not isinstance(region, (list, tuple)) or len(region) != 4:
                return {}, "参数 region 必须是 [x, y, width, height] 四个整数"
            try:
                region = [int(value) for value in region]
            except (TypeError, ValueError):
                return {}, "参数 region 必须是 [x, y, width, height] 四个整数"
            if region[2] <= 0 or region[3] <= 0:
                return {}, "参数 region 的 width/height 必须大于 0"
            prepared["region"] = region
        prepared["window"] = str(prepared.get("window") or "").strip()[:120]

    elif action == "click_text":
        text = str(prepared.get("text", "")).strip()
        if not text:
            return {}, "参数 text 不能为空"
        prepared["text"] = text[:120]
        index, error = _as_int(prepared.get("index", 0), "index")
        if error:
            return {}, error
        prepared["index"] = min(max(index or 0, 0), 20)
        prepared["window"] = str(prepared.get("window") or "").strip()[:120]

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


def _region_mapping_note(region: list[int], image_w: int, image_h: int, scale: float) -> str:
    """区域截图的坐标换算说明。

    区域截图返回的是一块“局部图片”，图片像素从 (0,0) 开始；如果直接把它当
    屏幕坐标去点，就会落到屏幕左上角附近（真实事故：小窗口在右下角，模型按
    裁剪图坐标点击，点到了左上角）。所以必须显式给出偏移与比例。
    """
    x, y, w, h = (int(value) for value in region)
    sx = w / image_w if image_w else 1.0
    sy = h / image_h if image_h else 1.0
    scale = scale or 1.0
    if abs(sx - 1.0) > 1e-6 or abs(sy - 1.0) > 1e-6:
        factor = f"x×{sx:.4f}、y×{sy:.4f}"
    else:
        factor = f"×{scale:.4f}"
    return (
        f"这是区域 [x={x}, y={y}, w={w}, h={h}] 的局部截图，图片 {image_w}x{image_h}；"
        f"屏幕坐标 = ({x} + 图片x {factor}, {y} + 图片y {factor})。"
        "不要把图片像素直接当屏幕坐标；要点文字/控件优先用 ocr_text / click_text / ui_tree。"
    )


def _screenshot_image_uri(result: Any, region: list[int] | None = None) -> tuple[str, str]:
    """返回 (data_uri, 尺寸提示)。

    OmniPilot 默认把截图缩到最长边 1568px；如果直接把缩放图发给模型，模型按图片
    像素估出来的坐标会和真实屏幕坐标不一致。这里优先读 OmniPilot 落盘的原图，
    让图片像素与屏幕一一对应；超大屏（如 4K）才回退到缩放图，并明确给出比例。

    `region` 不为空时额外给出“区域左上角偏移”，否则区域截图会被误当成全局坐标。
    """
    data = result.data or {}
    width = int(data.get("width") or 0)
    height = int(data.get("height") or 0)
    path = data.get("path")
    region = region if isinstance(region, list) and len(region) == 4 else None

    def _note(image_w: int, image_h: int, target_w: int, target_h: int) -> str:
        if region:
            scale = (target_w / image_w) if image_w and target_w else 1.0
            return _region_mapping_note(region, image_w, image_h, scale)
        if image_w != target_w or image_h != target_h:
            scale = (target_w / image_w) if image_w and target_w else 1.0
            return (
                f"图片已缩小到 {image_w}x{image_h}，屏幕实际为 {target_w}x{target_h}；"
                f"点击坐标 = 图片坐标 × {scale:.4f}。"
            )
        return (
            f"图片为原始分辨率 {image_w}x{image_h}，与屏幕坐标一致，可直接按图片像素估坐标。"
        )

    if path and width and height and width * height <= _MAX_FULL_IMAGE_PIXELS:
        try:
            raw = Path(path).read_bytes()
            encoded = base64.b64encode(raw).decode("ascii")
            return (f"data:image/png;base64,{encoded}", _note(width, height, width, height))
        except OSError:
            pass

    uri = result.image.data_uri()
    try:
        from PIL import Image

        with Image.open(io.BytesIO(result.image.raw_bytes())) as image:
            scaled_width, scaled_height = image.size
        hint = _note(scaled_width, scaled_height, width or scaled_width, height or scaled_height)
    except Exception:
        hint = "图片可能已缩小；点击前请用 screen_size 核对分辨率。"
    return uri, hint


def _format_result(action: str, result: Any, region: list[int] | None = None) -> str | list[dict]:
    """把 OmniPilot 的 ToolResult 转成 LangChain 可接受的工具输出。"""
    text = result.message_for_model()
    if not result.success:
        return _fail(result.error or text or "未知错误")

    if action == "screenshot" and result.image is not None:
        uri, size_hint = _screenshot_image_uri(result, region)
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


def _capture_for_ocr(region: list[int] | None) -> tuple[list[dict], str]:
    """截一张图并 OCR，返回已换算成屏幕坐标的文字行。"""
    tool_instance = _tool_instance("screenshot")
    if tool_instance is None:  # pragma: no cover - screenshot 总是可用
        return [], "截图工具不可用"
    payload: dict[str, Any] = {}
    if region:
        payload["region"] = list(region)
    try:
        result = tool_instance.execute(ToolContext(logger=logger), payload)
    except Exception as exc:  # pragma: no cover
        return [], f"截图失败：{exc}"
    if not result.success:
        return [], f"截图失败：{result.error or '未知错误'}"

    data = result.data or {}
    path = str(data.get("path") or "")
    if not path:
        return [], "截图没有返回文件路径，无法 OCR"
    try:
        image_width = int(data.get("width") or 0)
    except (TypeError, ValueError):
        image_width = 0

    try:
        ocr = ui_bridge.ocr_image(path)
    except UiBridgeError as exc:
        return [], f"OCR 失败：{exc}"

    lines = ocr.get("lines") or []
    pixel_width = int(ocr.get("image_w") or 0)
    # OCR 坐标是图片像素；截图被缩放时要按比例换回屏幕坐标
    if image_width and pixel_width:
        scale = image_width / pixel_width if region is None else (region[2] / pixel_width)
    else:
        scale = 1.0
    offset_x = 0 if region is None else int(region[0])
    offset_y = 0 if region is None else int(region[1])
    mapped = _map_ocr_lines(lines, scale, offset_x, offset_y)
    if not mapped:
        return [], "OCR 没有识别到可用文字（可能是纯图标界面）"
    return mapped, ""


def _format_ocr_lines(lines: list[dict]) -> str:
    head = (
        f"屏幕文字 {len(lines)} 行（cx/cy 是每行中心点，可直接用于 click；"
        f"内容可能包含隐私）："
    )
    body = [
        f"- \"{line['text'][:50]}\" cx={line['cx']} cy={line['cy']} "
        f"rect=({line['x']},{line['y']},{line['w']},{line['h']})"
        for line in lines[:_MAX_OCR_LINES]
    ]
    if len(lines) > _MAX_OCR_LINES:
        body.append(f"…（另有 {len(lines) - _MAX_OCR_LINES} 行未列出，可用 region 缩小范围）")
    return "\n".join([head, *body])


def _resolve_ui_target(prepared: dict[str, Any]) -> tuple[dict, str]:
    """先解析目标窗口并过守卫，再允许读/操作。

    顺序很重要：必须“先确认能碰，再动手”。否则控件的 Invoke 已经发出去了，
    才发现目标是个终端窗口，那时点已经点下去了。
    """
    try:
        info = ui_bridge.find_window(
            window=str(prepared.get("window") or ""), pid=prepared.get("pid")
        )
    except UiBridgeError as exc:
        if "window not found" in str(exc):
            target = prepared.get("window") or (
                f"pid={prepared.get('pid')}" if prepared.get("pid") else "当前前台窗口"
            )
            return {}, f"没找到目标窗口「{target}」，先用 ui_windows 确认窗口标题或 pid"
        return {}, f"解析目标窗口失败：{exc}"
    error = _guard_ui_target(info)
    if error:
        return {}, error
    return info, ""


def _ui_windows(prepared: dict[str, Any]) -> str:
    try:
        rows = ui_bridge.list_windows(
            limit=int(prepared.get("limit") or _MAX_UI_WINDOWS),
            filter_text=str(prepared.get("name_filter") or ""),
        )
    except UiBridgeError as exc:
        return _fail(f"读取窗口列表失败：{exc}")
    if not rows:
        return "没有找到可见窗口（可能全部最小化）。"
    return _format_ui_windows(rows)


def _ui_tree(prepared: dict[str, Any]) -> str:
    info, error = _resolve_ui_target(prepared)
    if error:
        return _fail(error)
    try:
        data = ui_bridge.dump_tree(
            window=str(info.get("title") or ""),
            pid=info.get("pid"),
            depth=int(prepared.get("depth") or 3),
            limit=int(prepared.get("limit") or _MAX_UI_NODES),
            filter_text=str(prepared.get("name_filter") or ""),
        )
    except UiBridgeError as exc:
        return _fail(f"读取控件树失败：{exc}")
    return _format_ui_tree(data)


def _ui_wait(prepared: dict[str, Any]) -> str:
    timeout = float(prepared.get("timeout") or 10.0)
    name = str(prepared.get("name") or "")
    aid = str(prepared.get("automation_id") or "")
    need_control = bool(name or aid)
    started = time.time()
    deadline = started + timeout
    last_note = ""
    while True:
        info: dict = {}
        try:
            info = ui_bridge.find_window(
                window=str(prepared.get("window") or ""), pid=prepared.get("pid")
            )
        except UiBridgeError as exc:
            last_note = str(exc)
            if "window not found" not in last_note:
                return _fail(f"等待失败：{last_note}")
        if info:
            error = _guard_ui_target(info)
            if error:
                return _fail(error)
            if not need_control:
                elapsed = time.time() - started
                # 只报“就绪了没 + pid”，不回显标题：否则等于不弹确认就读了窗口信息
                return (
                    f"目标窗口已就绪（pid={info.get('pid')}，{elapsed:.1f} 秒）。"
                    "下一步可以 ui_tree 看控件或直接截图。"
                )
            try:
                data = ui_bridge.dump_tree(
                    window=str(info.get("title") or ""),
                    pid=info.get("pid"),
                    depth=3,
                    limit=60,
                )
            except UiBridgeError as exc:
                last_note = str(exc)
                data = None
            if data:
                needle = (name or aid).lower()
                for node in data.get("nodes") or []:
                    haystack = f"{node.get('name') or ''} {node.get('aid') or ''}".lower()
                    if needle and needle in haystack:
                        elapsed = time.time() - started
                        # 不回显坐标/完整名字：只说明“你要等的东西出现了”，避免不弹确认就读屏幕内容
                        return (
                            f"已等到控件「{name or aid}」出现（{elapsed:.1f} 秒）。"
                            "要拿位置请用 ui_tree，要确认画面就截图。"
                        )
        if time.time() >= deadline:
            target = f"窗口或控件「{name or aid}」" if need_control else "目标窗口"
            return f"等了 {timeout:g} 秒仍未找到{target}。请截图确认当前画面，不要盲猜。"
        time.sleep(0.5)


def _ui_click(prepared: dict[str, Any]) -> str:
    info, error = _resolve_ui_target(prepared)
    if error:
        return _fail(error)
    try:
        data = ui_bridge.invoke_element(
            window=str(info.get("title") or ""),
            pid=info.get("pid"),
            name=str(prepared.get("name") or ""),
            aid=str(prepared.get("automation_id") or ""),
            control_type=str(prepared.get("control_type") or ""),
        )
    except UiBridgeError as exc:
        return _fail(f"按控件点击失败：{exc}")
    element = data.get("element") or {}
    label = str(element.get("name") or element.get("aid") or "控件")
    if data.get("how") == "invoke":
        return f"已按控件名触发「{label}」（InvokePattern，没有移动鼠标）。请截图确认结果。"
    x = int(data.get("x") or 0)
    y = int(data.get("y") or 0)
    click_error = _click_at(x, y)
    if click_error:
        return _fail(f"控件「{label}」没有 InvokePattern，点击其中心时失败：{click_error}")
    return f"控件「{label}」没有 InvokePattern，已点击其中心 ({x}, {y})。请截图确认结果。"


def _ui_set_text(prepared: dict[str, Any]) -> str:
    text = str(prepared.get("text") or "")
    info, error = _resolve_ui_target(prepared)
    if error:
        return _fail(error)
    try:
        data = ui_bridge.set_element_value(
            window=str(info.get("title") or ""),
            pid=info.get("pid"),
            name=str(prepared.get("name") or ""),
            aid=str(prepared.get("automation_id") or ""),
            control_type=str(prepared.get("control_type") or ""),
            text=text,
        )
    except UiBridgeError as exc:
        return _fail(f"写入控件失败：{exc}")
    element = data.get("element") or {}
    label = str(element.get("name") or element.get("aid") or "控件")
    if data.get("how") == "value":
        return f"已直接写入控件「{label}」（ValuePattern.SetValue），没有模拟键盘。请截图确认内容。"

    x = int(data.get("x") or 0)
    y = int(data.get("y") or 0)
    click_error = _click_at(x, y)
    if click_error:
        return _fail(f"控件「{label}」不支持直接赋值，点击其中心也失败：{click_error}")
    select_error = _hotkey(["ctrl", "a"])
    if select_error:
        return _fail(f"已点击控件但全选失败：{select_error}")
    type_error = _type_into_focus(text)
    if type_error:
        return _fail(f"已点击控件但输入失败：{type_error}")
    return (
        f"控件「{label}」不支持直接赋值：已点击其中心、全选并键入 {len(text)} 个字符。"
        "请截图确认内容是否正确（可能被原有内容叠加）。"
    )


def _ocr_text(prepared: dict[str, Any]) -> str:
    region = prepared.get("region")
    region = region if isinstance(region, list) else None
    window = str(prepared.get("window") or "").strip()
    if window:
        info, error = _resolve_ui_target(prepared)
        if error:
            return _fail(error)
        left_raw = int(info.get("x") or 0)
        top_raw = int(info.get("y") or 0)
        width = int(info.get("w") or 0)
        height = int(info.get("h") or 0)
        if width <= 0 or height <= 0:
            return _fail("目标窗口尺寸为 0（可能已最小化），无法 OCR")
        # 最大化窗口的 x/y 可能是负数，裁剪成屏幕内区域
        left = max(0, left_raw)
        top = max(0, top_raw)
        region = [left, top, max(1, width - (left - left_raw)), max(1, height - (top - top_raw))]
    lines, error = _capture_for_ocr(region)
    if error:
        return _fail(error)
    return _format_ocr_lines(lines)


def _click_text(prepared: dict[str, Any]) -> str:
    region = None
    window = str(prepared.get("window") or "").strip()
    if window:
        info, error = _resolve_ui_target(prepared)
        if error:
            return _fail(error)
        left_raw = int(info.get("x") or 0)
        top_raw = int(info.get("y") or 0)
        width = int(info.get("w") or 0)
        height = int(info.get("h") or 0)
        if width <= 0 or height <= 0:
            return _fail("目标窗口尺寸为 0（可能已最小化），无法 OCR")
        left = max(0, left_raw)
        top = max(0, top_raw)
        region = [left, top, max(1, width - (left - left_raw)), max(1, height - (top - top_raw))]
    lines, error = _capture_for_ocr(region)
    if error:
        return _fail(error)
    line, error = _select_ocr_line(lines, str(prepared.get("text") or ""), int(prepared.get("index") or 0))
    if error:
        return _fail(error)
    assert line is not None
    click_error = _click_at(int(line["cx"]), int(line["cy"]))
    if click_error:
        return _fail(f"点击文字「{line['text']}」失败：{click_error}")
    return (
        f"已点击屏幕上包含「{prepared.get('text')}」的文字：\"{line['text'][:40]}\" "
        f"中心 ({line['cx']}, {line['cy']})。请截图确认结果。"
    )


def _run_ui_action(action: str, prepared: dict[str, Any]) -> str:
    if not ui_bridge.available():
        return _fail(
            "UI 自动化当前不可用（需要 Windows + PowerShell + agent/tools/uia_bridge.ps1）"
        )
    handlers = {
        "ui_windows": _ui_windows,
        "ui_tree": _ui_tree,
        "ui_wait": _ui_wait,
        "ui_click": _ui_click,
        "ui_set_text": _ui_set_text,
        "ocr_text": _ocr_text,
        "click_text": _click_text,
    }
    handler = handlers.get(action)
    if handler is None:  # pragma: no cover - 由 _validate_action 兜底
        return _fail(f"未知动作 {action}")
    logger.info(f"[computer_use]UI 自动化 {action}，目标窗口 {prepared.get('window') or prepared.get('pid') or '前台'}")
    return handler(prepared)


@tool(description=(
    "控制本机真实桌面（OmniPilot 工具层 + Windows UI 自动化 / 内置 OCR，单工具多动作）。"
    "优先用“控件”而不是“猜像素”："
    "① UI 自动化：ui_windows（列窗口）、ui_tree（看控件树，带名字/类型/位置/状态）、"
    "ui_wait（等窗口或控件出现，不靠 sleep 硬等）、ui_click（按控件名点击）、ui_set_text（按控件名写文字）；"
    "② OCR：ocr_text（读出屏幕上的文字与坐标）、click_text（按屏幕文字点击）；"
    "③ 像素：screenshot、screen_size、locate_image、wait、click、double_click、right_click、move_mouse、drag、scroll；"
    "键盘：type_text、press_key、hotkey；系统：clipboard_get、clipboard_set、app_open、app_open_start_menu、app_close、app_list。"
    "推荐的桌面顺序：ui_windows 找窗口 → ui_tree 看控件 → ui_click/ui_set_text 操作 → 再截图或 ui_tree 确认；"
    "ui_tree 拿不到控件（自绘界面、部分网页/游戏）时用 ocr_text / click_text；坐标点击是最后选择。"
    "ui_click / ui_set_text / click_text 会拒绝终端、脚本宿主、注册表等系统工具窗口，也不会写密码框；"
    "这些动作都会先请求用户确认，其中点击/输入还需要 PIN。"
    "只有 screen_size、locate_image、wait、ui_wait 直接执行。"
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
    text: Annotated[str | None, "要输入/写入的文字或要查找的文字。type_text、clipboard_set、ui_set_text、click_text 使用。"] = None,
    interval: Annotated[float, "逐字输入间隔秒数，0 表示用剪贴板粘贴（支持中文）。type_text 使用。"] = 0.0,
    key: Annotated[str | None, "按键名，如 enter、tab、escape、f5、up、down。press_key 使用。"] = None,
    presses: Annotated[int, "按键次数，1-20。press_key 使用，默认 1。"] = 1,
    keys: Annotated[list[str] | str | None, "组合键，如 ['ctrl','s'] 或 'ctrl+s'。hotkey 使用。"] = None,
    name: Annotated[str | None, "应用名称（app_open 系）或控件名字（ui_click / ui_set_text / ui_wait 使用，中英文均可，支持部分匹配）。"] = None,
    arguments: Annotated[list[str] | str | None, "启动应用时的命令行参数。app_open 使用。"] = None,
    force: Annotated[bool, "关闭应用时是否强制结束。app_close 使用，默认 false。"] = False,
    name_filter: Annotated[str | None, "按名字过滤：app_list 过滤应用名，ui_windows 过滤窗口标题，ui_tree 过滤控件名。"] = None,
    limit: Annotated[int, "返回数量上限。app_list 默认 50（1-200）；ui_windows 默认 40；ui_tree 默认 120。"] = 50,
    image_path: Annotated[str | None, "要在屏幕上查找的模板 PNG 路径，必须在工作区域内。locate_image 使用。"] = None,
    confidence: Annotated[float | None, "图片匹配阈值 0-1；省略则精确匹配（无需 opencv-python）。locate_image 使用。"] = None,
    region: Annotated[list[int] | None, "区域 [x, y, width, height]，省略为全屏。screenshot / ocr_text 使用。"] = None,
    attach: Annotated[bool, "截图是否附加给模型查看，默认 true。screenshot 使用。"] = True,
    seconds: Annotated[float, "等待秒数，0-10，默认 1。wait 使用。"] = 1.0,
    window: Annotated[str | None, "目标窗口标题片段（如 '微信'、'记事本'），省略则用当前前台窗口。UI 自动化动作使用。"] = None,
    pid: Annotated[int | None, "目标窗口的进程号，比标题更精确。UI 自动化动作使用（与 window 二选一）。"] = None,
    automation_id: Annotated[str | None, "控件的 AutomationId（比名字更稳定）。ui_click / ui_set_text / ui_wait 使用。"] = None,
    control_type: Annotated[str | None, "控件类型过滤，如 Button、Edit、MenuItem。ui_click / ui_set_text 可选。"] = None,
    depth: Annotated[int, "控件树深度 1-6，默认 3。ui_tree 使用。"] = 3,
    timeout: Annotated[float, "等待秒数 0-30，默认 10。ui_wait 使用。"] = 10.0,
    index: Annotated[int, "多选时的序号（从 0 开始），默认 0。click_text 用于选第几处文字。"] = 0,
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
        "window": window,
        "pid": pid,
        "automation_id": automation_id,
        "control_type": control_type,
        "depth": depth,
        "timeout": timeout,
        "index": index,
    }
    prepared, error = _prepare_arguments(action, raw_args)
    if error:
        return _fail(error)

    if action == "wait":
        time.sleep(float(prepared["seconds"]))
        return f"已等待 {prepared['seconds']:g} 秒。"

    if action in _UI_ACTIONS:
        return _run_ui_action(action, prepared)

    if action == "app_open_start_menu":
        logger.info(f"[computer_use]从开始菜单启动应用：{prepared['name']}")
        return _launch_start_menu_app(str(prepared["name"]))

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
    formatted = _format_result(action, result, region=prepared.get("region"))

    # app_open 在 Windows 上对友好名走 `cmd /c start`（即发即忘），拿不到真实成败。
    # 不在此处自动回退（会误把“启动成功”当成“未运行”造成重复启动，也会把未确认说成失败）；
    # 只附一句中性引导：若用户反馈“打不开/没打开”且是 Windows，改用 app_open_start_menu。
    if (
        action == "app_open"
        and isinstance(formatted, str)
        and not formatted.startswith("桌面控制失败")
    ):
        formatted = (
            f"{formatted}\n（若用户反馈没打开/打不开，且本机是 Windows，"
            "请改用 computer_use(action=app_open_start_menu, name=...) 从开始菜单按中英文名启动）"
        )
    return formatted


if __name__ == "__main__":
    # 手工冒烟：只测试只读动作，避免误触鼠标键盘。
    print(computer_use.invoke({"action": "screen_size"}))
    print(computer_use.invoke({"action": "app_list", "limit": 5}))
    print(computer_use.invoke({"action": "wait", "seconds": 0.1}))
