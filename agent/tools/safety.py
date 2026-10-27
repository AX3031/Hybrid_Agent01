"""电脑操作的安全策略与小白化解释。

所有接路径的工具都必须先经过 `guard_path()`：
1. 展开为绝对路径；
2. 必须落在 `config/agent.yml` 配置的工作区域内；
3. 不能命中敏感目录。

写/删类操作还会用到：
- `check_write_suffix()`：拒绝写入可执行文件；
- `backup_before_overwrite()`：覆盖前自动备份；
- `move_to_trash()`：删除前移入回收区。

命令执行会用到 `check_command()`：危险命令黑名单 + 工作区外路径提取。
"""

import os
import re
import shutil
from datetime import datetime
from pathlib import Path

import yaml

from utils.config_handler import agent_conf
from utils.path_tool import get_abs_path, get_project_root

_CONFIG_PATH = Path(get_abs_path("config/agent.yml"))

# ---------------------------------------------------------------------------
# 路径展开与工作区域
# ---------------------------------------------------------------------------


def expand_path(raw: str) -> Path:
    """展开 ~、环境变量和包裹引号，返回绝对路径。

    相对路径统一基于项目根解析，避免运行目录不同导致指向不同位置。
    """
    text = str(raw).strip().strip('"').strip("'")
    if not text:
        text = "."
    if text == "~":
        return Path.home().resolve()
    text = os.path.expandvars(os.path.expanduser(text))
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = Path(get_project_root()) / candidate
    return candidate.resolve()


def _norm(path: Path) -> str:
    """大小写不敏感、并展开 Windows 8.3 短路径后的比较键。"""
    return os.path.normcase(os.path.realpath(os.path.abspath(str(path))))


def _is_under(path: Path, root: Path) -> bool:
    """大小写不敏感的前缀判断，兼容 Windows 路径。"""
    child, parent = _norm(path), _norm(root)
    return child == parent or child.startswith(parent + os.sep)


def _read_config() -> dict:
    """直接读取 agent.yml，保证界面保存后能立即拿到最新值。"""
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as stream:
            return yaml.safe_load(stream) or {}
    except OSError:
        return {}


def _load_workspace_roots() -> list[Path]:
    """读取工作区域配置；为空时默认「用户目录 + 项目根目录」。"""
    configured = _read_config().get("workspace_roots") or ["~", "."]
    roots: list[Path] = []
    for item in configured:
        text = str(item).strip()
        if not text:
            continue
        if text == "~":
            candidate = Path.home().resolve()
        elif text == ".":
            candidate = Path(get_project_root()).resolve()
        else:
            candidate = expand_path(text)
        if candidate not in roots:
            roots.append(candidate)
    return roots


WORKSPACE_ROOTS = _load_workspace_roots()

_SENSITIVE_NAMES = tuple(
    str(name).strip().lower()
    for name in (
        agent_conf.get("sensitive_dirs")
        or [".ssh", ".gnupg", ".aws", ".azure", ".kube"]
    )
    if str(name).strip()
)

# 系统保护目录：即使被配置进工作区，也拒绝写入/删除
_PROTECTED_ROOTS = [
    Path(path).resolve()
    for path in (
        os.environ.get("SystemRoot", r"C:\Windows"),
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("ProgramData", r"C:\ProgramData"),
    )
    if path
]

# 拒绝写入的可执行文件后缀，防止「写脚本再执行」的绕过链路
_BLOCKED_WRITE_SUFFIXES = {
    ".exe", ".dll", ".sys", ".bat", ".cmd", ".ps1", ".vbs", ".vbe",
    ".js", ".jse", ".wsf", ".wsh", ".scr", ".msi", ".com", ".cpl", ".jar",
}

# 覆盖前备份的大小上限
_BACKUP_MAX_BYTES = 10 * 1024 * 1024

# Agent 自己的状态/备份/回收目录：只允许读，不允许写，防止模型篡改批次记录或备份。
_RESERVED_DIR_NAMES = (".agent_state", ".agent_backup", ".agent_trash")

# 项目自身的源码/配置/提示词/脚本/日志/虚拟环境与 Git 元数据：Agent 只能读，不能写。
# 这里用相对项目根的路径判断，避免把同名目录（例如工作区里的 logs）误伤。
_PROJECT_ROOT = Path(get_project_root()).resolve()
_PROJECT_PROTECTED_DIRS = frozenset(
    {
        "agent",
        "config",
        "prompts",
        "scripts",
        "utils",
        "model",
        "memorys",
        "env_patch",
        "tests",
        "logs",
        ".git",
        ".github",
        ".venv",
        ".idea",
        ".vscode",
    }
)
# 项目内需要整体保护、但不属于顶层目录的路径。
# 技能库含用户保存的步骤和截图，Agent 只能作为参考读取，不能改写。
_PROJECT_PROTECTED_RELATIVE_DIRS = frozenset({"data/task_skills"})

_PROJECT_PROTECTED_FILES = frozenset(
    {
        "app.py",
        "app_file_uploader.py",
        "main.py",
        "test1.py",
        "pyproject.toml",
        "uv.lock",
        "setup.py",
        "setup.cfg",
        "pytest.ini",
        "tox.ini",
        "AGENTS.md",
        "README.md",
        "CHANGELOG.md",
        ".gitignore",
        ".env",
    }
)

_PROJECT_PATH_TOKEN = re.compile(
    r"(?i)(?<![a-z0-9_.-])(agent|config|prompts|scripts|utils|model|memorys|env_patch|tests|logs|\.venv|\.git|\.github)"
    r"(?:[\\/]|(?=$|[\s\"'&|;]))"
)
_PROJECT_FILE_TOKEN = re.compile(
    r"(?i)(?<![a-z0-9_.-])(app\.py|app_file_uploader\.py|main\.py|test1\.py|"
    r"pyproject\.toml|uv\.lock|setup\.py|setup\.cfg|pytest\.ini|tox\.ini|"
    r"agents\.md|readme\.md|changelog\.md)(?=$|[\s\"'&|;<>])"
)
_PROJECT_ENV_TOKEN = re.compile(
    r"(?i)(?<![a-z0-9_.-])\.env(?:\.[a-z0-9_.-]+)?(?![a-z0-9_.-])"
)
_PROJECT_SKILL_TOKEN = re.compile(r"(?i)(?<![a-z0-9_.-])task_skills(?![a-z0-9_.-])")


def is_project_protected(path: Path) -> bool:
    """判断路径是否属于项目源码/配置/提示词/脚本/日志/虚拟环境/Git 元数据。"""
    try:
        relative = path.resolve().relative_to(_PROJECT_ROOT)
    except (OSError, ValueError):
        return False
    parts = [part.lower() for part in relative.parts]
    if not parts:
        return False
    relative_dir = "/".join(parts)
    if any(
        relative_dir == item or relative_dir.startswith(item + "/")
        for item in _PROJECT_PROTECTED_RELATIVE_DIRS
    ):
        return True
    if parts[0] in _PROJECT_PROTECTED_DIRS:
        return True
    if len(parts) == 1:
        if parts[0] in _PROJECT_PROTECTED_FILES:
            return True
        if parts[0] == ".env" or parts[0].startswith(".env."):
            return True
    return False


def _command_touches_project_protected(command: str) -> bool:
    """命令文本是否引用项目受保护路径。"""
    return bool(
        _PROJECT_PATH_TOKEN.search(command)
        or _PROJECT_FILE_TOKEN.search(command)
        or _PROJECT_ENV_TOKEN.search(command)
        or _PROJECT_SKILL_TOKEN.search(command)
    )


def workspace_roots() -> list[Path]:
    """返回当前生效的工作区域列表。"""
    return list(WORKSPACE_ROOTS)


def reload_workspace_roots() -> list[Path]:
    """重新读取 agent.yml 并更新全局工作区域，使其立即生效。"""
    global WORKSPACE_ROOTS
    WORKSPACE_ROOTS = _load_workspace_roots()
    return list(WORKSPACE_ROOTS)


def validate_workspace_inputs(lines: list[str]) -> tuple[list[str], list[str]]:
    """校验用户输入的工作区域路径，返回（合法路径, 错误说明）。"""
    valid: list[str] = []
    errors: list[str] = []

    for raw in lines:
        text = str(raw).strip().strip('"').strip("'")
        if not text:
            continue
        if text == "~":
            candidate = Path.home().resolve()
        elif text == ".":
            candidate = Path(get_project_root()).resolve()
        else:
            candidate = Path(text)
            if not candidate.is_absolute():
                errors.append(f"不是绝对路径：{text}")
                continue
            candidate = candidate.resolve()

        if len(candidate.parts) <= 1:
            errors.append(f"不能使用盘符根目录：{candidate}")
            continue
        if is_system_protected(candidate):
            errors.append(f"不能使用系统保护目录：{candidate}")
            continue
        if is_sensitive(candidate):
            errors.append(f"不能把敏感目录设为工作区域：{candidate}")
            continue

        normalized = str(candidate)
        if normalized not in valid:
            valid.append(normalized)

    if not valid:
        errors.append("至少需要保留一个有效的工作区域")
    return valid, errors


def save_workspace_roots(lines: list[str]) -> tuple[bool, str]:
    """把工作区域写回 config/agent.yml，并立即生效。"""
    valid, errors = validate_workspace_inputs(lines)
    if errors:
        return False, "；".join(errors)

    config = _read_config()
    config["workspace_roots"] = valid

    try:
        _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp_path = _CONFIG_PATH.with_suffix(".yml.tmp")
        with open(temp_path, "w", encoding="utf-8") as stream:
            yaml.safe_dump(config, stream, allow_unicode=True, sort_keys=False)
        os.replace(temp_path, _CONFIG_PATH)
    except OSError as exc:
        return False, f"保存配置失败：{exc}"

    # 同步内存中的配置，保证当前进程立即生效
    agent_conf["workspace_roots"] = valid
    reload_workspace_roots()

    missing = [root for root in valid if not Path(root).exists()]
    note = f"；以下路径当前不存在，暂时无法使用：{', '.join(missing)}" if missing else ""
    return True, f"已保存 {len(valid)} 个工作区域，立即生效{note}"


def workspace_text() -> str:
    """返回适合展示给用户的工作区域文本。"""
    return "\n".join(f"- {root}" for root in WORKSPACE_ROOTS)


def workspace_root_of(path: Path) -> Path | None:
    """返回路径所属的工作区域根；不在任何根内时返回 None。"""
    for root in WORKSPACE_ROOTS:
        if _is_under(path, root):
            return root
    return None


def in_workspace(path: Path) -> bool:
    return workspace_root_of(path) is not None


def is_workspace_root(path: Path) -> bool:
    """判断路径是否正好是某个工作区域的根目录。"""
    return any(_norm(path) == _norm(root) for root in WORKSPACE_ROOTS)


def is_sensitive(path: Path) -> bool:
    """判断路径是否经过敏感目录（如 .ssh、.aws）。"""
    parts = [part.lower() for part in path.parts]
    return any(name in parts for name in _SENSITIVE_NAMES)


def is_system_protected(path: Path) -> bool:
    """判断路径是否属于系统保护目录或盘符根目录。"""
    if len(path.parts) <= 1:
        return True
    return any(_is_under(path, root) for root in _PROTECTED_ROOTS)


def _approval_blocked(path: Path) -> bool:
    """审批凭据目录（PIN 哈希 / 签名密钥）对 Agent 完全不可见。"""
    try:
        from agent.guard.approval import is_protected_path

        return is_protected_path(path)
    except Exception:  # noqa: BLE001 - 审批模块不可用时不影响其他安全检查
        return False


def _is_reserved(path: Path) -> bool:
    """判断路径是否落在 Agent 自己的状态目录里。"""
    parts = [part.lower() for part in path.parts]
    return any(name in parts for name in _RESERVED_DIR_NAMES)


def guard_path(raw: str, *, for_write: bool = False) -> tuple[Path | None, str]:
    """路径的统一安全入口。

    返回 `(path, "")` 表示通过；`(None, 错误说明)` 表示拒绝。
    """
    path = expand_path(raw)
    if _approval_blocked(path):
        return None, f"该路径属于审批凭据目录，Agent 不得访问：{path}"
    if not in_workspace(path):
        return None, (
            f"不在允许的工作区域内，已拒绝：{path}\n"
            f"当前工作区域：\n{workspace_text()}\n"
            "如需访问其他目录，请修改 config/agent.yml 的 workspace_roots 后重启应用。"
        )
    if is_sensitive(path):
        return None, f"该路径属于敏感目录，已拒绝访问：{path}"
    if for_write and _is_reserved(path):
        return None, f"该路径属于 Agent 的状态/备份/回收目录，已拒绝修改：{path}"
    if for_write and is_project_protected(path):
        return None, (
            f"该路径属于项目源码/配置/提示词/脚本/日志/虚拟环境/Git 元数据，"
            f"Agent 不得写入：{path}"
        )
    if for_write and is_system_protected(path):
        return None, f"该路径属于系统保护目录，已拒绝修改：{path}"
    return path, ""


# ---------------------------------------------------------------------------
# 写入前检查、备份与回收区
# ---------------------------------------------------------------------------


def check_write_suffix(path: Path) -> str:
    """拒绝写入可执行文件类型；通过时返回空字符串。"""
    suffix = path.suffix.lower()
    if suffix in _BLOCKED_WRITE_SUFFIXES:
        return (
            f"出于安全考虑，拒绝写入可执行文件类型（{suffix}）：{path}\n"
            "如果确实需要脚本，请先用 .txt 保存，再自行重命名。"
        )
    return ""


def check_tree_write_suffix(path: Path) -> str:
    """检查目录树里是否含可执行后缀；用于复制/移动整个目录。"""
    if path.is_file():
        return check_write_suffix(path)
    if not path.is_dir():
        return ""
    for current_root, _dir_names, file_names in os.walk(path):
        for name in file_names:
            error = check_write_suffix(Path(current_root) / name)
            if error:
                return error
    return ""


def backup_before_overwrite(path: Path) -> tuple[str, Path | None]:
    """覆盖已有文件前先备份，返回（给用户看的说明, 备份路径）。"""
    if not path.exists() or not path.is_file():
        return "", None
    try:
        if path.stat().st_size > _BACKUP_MAX_BYTES:
            return "（原文件超过 10MB，未自动备份）", None
        root = workspace_root_of(path) or path.parent
        backup_dir = root / ".agent_backup"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = backup_dir / f"{stamp}_{path.name}"
        counter = 1
        while dest.exists():
            dest = backup_dir / f"{stamp}_{counter}_{path.name}"
            counter += 1
        shutil.copy2(path, dest)
        return f"（原文件已备份到 {dest}）", dest
    except OSError as exc:
        return f"（原文件备份失败：{exc}）", None


def move_to_trash(path: Path) -> tuple[Path | None, str]:
    """把文件或目录移到所在工作区的 .agent_trash 回收目录。"""
    root = workspace_root_of(path)
    if root is None:
        return None, "该路径不在工作区域内，无法移入回收区"
    if _norm(path) == _norm(root):
        return None, "不能删除工作区域根目录本身"

    try:
        trash = root / ".agent_trash"
        trash.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = trash / f"{stamp}_{path.name}"
        counter = 1
        while dest.exists():
            dest = trash / f"{stamp}_{counter}_{path.name}"
            counter += 1
        shutil.move(str(path), str(dest))
        return dest, ""
    except OSError as exc:
        return None, f"移入回收区失败：{exc}"


# ---------------------------------------------------------------------------
# 命令安全检查
# ---------------------------------------------------------------------------

_DANGEROUS_COMMANDS = [
    (re.compile(r"\bformat\b", re.IGNORECASE), "格式化磁盘"),
    (re.compile(r"\bdiskpart\b", re.IGNORECASE), "磁盘分区管理"),
    (re.compile(r"\bbcdedit\b", re.IGNORECASE), "修改启动配置"),
    (re.compile(r"\breg\s+delete\b", re.IGNORECASE), "删除注册表项"),
    (re.compile(r"\bshutdown\b", re.IGNORECASE), "关机或重启"),
    (re.compile(r"\b(del|erase)\b", re.IGNORECASE), "命令行删除（请使用 delete_path 进入回收区）"),
    (re.compile(r"\bRemove-Item\b", re.IGNORECASE), "PowerShell 删除（请使用 delete_path 进入回收区）"),
    (re.compile(r"\brm\b", re.IGNORECASE), "命令行删除（请使用 delete_path 进入回收区）"),
    (re.compile(r"\b(rd|rmdir)\b[^\n]*\s/s\b", re.IGNORECASE), "递归删除目录"),
    (re.compile(r"\btaskkill\b[^\n]*/f\b", re.IGNORECASE), "强制结束进程"),
    (re.compile(r"\bnet\s+user\b[^\n]*/add\b", re.IGNORECASE), "创建系统用户"),
    (re.compile(r"\bnetsh\b", re.IGNORECASE), "修改网络配置"),
    (re.compile(r"\bcipher\b[^\n]*/w\b", re.IGNORECASE), "擦除磁盘空闲空间"),
    (re.compile(r"\bvssadmin\b[^\n]*delete", re.IGNORECASE), "删除卷影副本"),
    (re.compile(r"\bschtasks\b[^\n]*/create\b", re.IGNORECASE), "创建计划任务"),
    # 以下为常见的绕过链路
    (re.compile(r"%\s*(SystemRoot|windir|ProgramFiles|ProgramData)\s*%", re.IGNORECASE), "引用系统目录环境变量"),
    (re.compile(r"-(EncodedCommand|enc)\b", re.IGNORECASE), "PowerShell 编码命令"),
    (re.compile(r"\b(cd|chdir|pushd|Set-Location)\b[^\n]*\.\.", re.IGNORECASE), "向上一级切换目录可能越出工作区"),
    (re.compile(r"\bcopy\s+con\b", re.IGNORECASE), "通过 copy con 创建文件（请使用 write_text_file）"),
    (re.compile(r">\s*\S+\.(exe|dll|bat|cmd|ps1|vbs|js|msi|scr|com)\b", re.IGNORECASE), "重定向写入可执行文件"),
    (re.compile(r"\b(Set-Content|Add-Content|Out-File)\b[^\n]*\.(exe|dll|bat|cmd|ps1|vbs|js|msi|scr|com)\b", re.IGNORECASE), "写入可执行文件（请使用 write_text_file）"),
    (re.compile(r"\b(certutil|bitsadmin|mshta|rundll32|regsvr32)\b", re.IGNORECASE), "下载或执行外部程序"),
    (re.compile(r"\^[A-Za-z]"), "cmd 转义符可能混淆命令"),
    (re.compile(r"\battrib\b", re.IGNORECASE), "修改文件属性"),
    (re.compile(r"\b(icacls|takeown)\b", re.IGNORECASE), "修改文件权限或所有权"),
    (re.compile(r"\bwmic\b", re.IGNORECASE), "WMI 操作"),
    (re.compile(r"\bStart-Process\b", re.IGNORECASE), "启动外部程序（请使用 open_path）"),
    (re.compile(r"\b(Invoke-WebRequest|Invoke-Expression|iwr|iex)\b", re.IGNORECASE), "下载或执行脚本"),
    (re.compile(
        r"\b(copy|move|xcopy|robocopy)\b[^\n]*\.(exe|dll|bat|cmd|ps1|vbs|js|msi|scr|com)\b",
        re.IGNORECASE,
    ), "复制/移动为可执行文件"),
]

_WIN_ABS_PATH = re.compile(r"([A-Za-z]:[\\/][^\s\"'|<>]*)")


def check_command(command: str) -> str:
    """检查命令是否命中黑名单或引用工作区外路径；通过时返回空字符串。"""
    text = str(command).strip()
    if not text:
        return "命令不能为空"

    lowered = text.lower()
    for name in _RESERVED_DIR_NAMES:
        if name in lowered:
            return f"该命令访问了 Agent 的状态/备份/回收目录（{name}），已拒绝：{text}"
    if _command_touches_project_protected(text):
        return (
            "该命令引用了项目源码/配置/提示词/脚本/日志/虚拟环境/Git 元数据/技能库，"
            f"已拒绝：{text}"
        )
    try:
        from agent.guard.approval import approval_dir

        if os.path.normcase(str(approval_dir())) in os.path.normcase(text):
            return f"该命令访问了审批凭据目录，已拒绝：{text}"
    except Exception:  # noqa: BLE001 - 审批模块不可用时继续其他检查
        pass

    for pattern, label in _DANGEROUS_COMMANDS:
        if pattern.search(text):
            return f"该命令被安全策略拦截（识别为：{label}）：{text}"

    for match in _WIN_ABS_PATH.finditer(text):
        raw = match.group(1).rstrip("\\.,;")
        if len(raw) <= 2:  # 例如单独一个 C:，不当作越界路径
            continue
        try:
            path = Path(raw).resolve()
        except OSError:
            continue
        if not in_workspace(path):
            return (
                f"命令中包含工作区域外的路径，已拒绝：{raw}\n"
                f"当前工作区域：\n{workspace_text()}"
            )
    return ""


# ---------------------------------------------------------------------------
# 给普通用户看的解释
# ---------------------------------------------------------------------------


def _preview(text: object, limit: int = 24) -> str:
    """把要展示或输入的文字压成一行短预览。"""
    value = str(text or "").replace("\r", " ").replace("\n", " ").strip()
    return value if len(value) <= limit else f"{value[:limit]}…"


def _describe_computer_use(action: str, args: dict) -> tuple[str, str]:
    """computer_use 的人话标题与说明。"""
    x, y = args.get("x", "?"), args.get("y", "?")
    if action == "screenshot":
        target = f"区域 {args.get('region')}" if args.get("region") else "整个屏幕"
        return "截取屏幕画面", f"截取{target}交给模型查看。截图可能包含密码、聊天记录等隐私内容。"
    if action == "screen_size":
        return "查看屏幕分辨率", "读取当前屏幕的像素尺寸，不截取画面。"
    if action == "locate_image":
        return "在屏幕上查找图片", f"在屏幕上查找模板图片「{args.get('image_path', '')}」。"
    if action == "wait":
        return "等待界面响应", f"等待 {args.get('seconds', 1)} 秒，不修改任何内容。"
    if action == "click":
        return (
            "点击屏幕",
            f"在坐标 ({x}, {y}) 用 {args.get('button', 'left')} 键点击 {args.get('clicks', 1)} 次。",
        )
    if action == "double_click":
        return "双击屏幕", f"在坐标 ({x}, {y}) 双击。"
    if action == "right_click":
        return "右键点击", f"在坐标 ({x}, {y}) 右键点击。"
    if action == "move_mouse":
        return "移动鼠标", f"把鼠标移动到坐标 ({x}, {y})，不点击。"
    if action == "drag":
        return (
            "拖拽鼠标",
            f"从 ({args.get('x1')}, {args.get('y1')}) 拖到 ({args.get('x2')}, {args.get('y2')})。",
        )
    if action == "scroll":
        where = f"，位置 ({x}, {y})" if x != "?" else ""
        return "滚动鼠标滚轮", f"滚动 {args.get('amount')} 格{where}。"
    if action == "type_text":
        return (
            "输入文字",
            (
                f"向当前焦点窗口输入「{_preview(args.get('text', ''))}」。"
                "文字会落到当前焦点窗口，且输入过程会覆盖剪贴板。"
            ),
        )
    if action == "press_key":
        return "按键", f"按下「{args.get('key', '')}」{args.get('presses', 1)} 次。"
    if action == "hotkey":
        keys = args.get("keys") or []
        if isinstance(keys, str):
            keys = [keys]
        return "按下组合键", f"按下「{'+'.join(str(k) for k in keys)}」。"
    if action == "clipboard_get":
        return "读取剪贴板", "读取当前剪贴板里的文字（可能包含密码、验证码等敏感信息）。"
    if action == "clipboard_set":
        return (
            "写入剪贴板",
            f"把「{_preview(args.get('text', ''))}」写入剪贴板，原内容会被覆盖。",
        )
    if action == "app_open":
        return "启动应用", f"打开「{args.get('name', '')}」。"
    if action == "app_close":
        force = "（强制结束）" if args.get("force") else ""
        return "关闭应用", f"关闭「{args.get('name', '')}」{force}。"
    if action == "app_list":
        return "查看运行中的应用", "列出当前运行的应用/进程，可能暴露你正在使用哪些软件。"
    return "控制桌面", "执行了一个桌面控制动作。"


def _computer_use_risk(action: str) -> str:
    if action == "screenshot":
        return "截图可能包含密码、聊天记录、邮件等隐私内容，并会作为图片发送给模型分析。"
    if action == "clipboard_get":
        return "剪贴板可能包含密码、验证码、密钥等敏感信息。"
    if action in {"type_text", "clipboard_set"}:
        return (
            "会向当前焦点窗口输入/写入文字；如果焦点窗口不对，文字可能落到别处。"
            "type_text 会覆盖剪贴板。"
        )
    if action in {"click", "double_click", "right_click", "drag", "scroll"}:
        return "会在真实屏幕上操作鼠标；坐标错误可能误点其他按钮或拖错窗口。"
    if action == "app_open":
        return "会启动本机程序。工具会拒绝终端、脚本宿主、注册表和磁盘工具。"
    if action == "app_close":
        return "会终止目标程序，未保存的数据可能丢失。系统关键进程会被拒绝关闭。"
    if action == "app_list":
        return "会读取当前运行的程序列表，可能暴露你正在使用哪些软件。"
    if action in {"screen_size", "locate_image", "wait"}:
        return "只读取屏幕尺寸/查找图片/等待，不修改任何内容。"
    return "会操作真实桌面，请确认坐标、目标窗口和文字内容。"


def _computer_use_target(action: str, args: dict) -> str:
    if action == "screenshot":
        return str(args.get("region") or "整个屏幕")
    if action in {"click", "double_click", "right_click", "move_mouse"}:
        return f"({args.get('x')}, {args.get('y')})"
    if action == "drag":
        return f"({args.get('x1')},{args.get('y1')}) -> ({args.get('x2')},{args.get('y2')})"
    if action in {"type_text", "clipboard_set"}:
        return _preview(args.get("text", ""), 80)
    if action in {"app_open", "app_close"}:
        return str(args.get("name", ""))
    if action == "locate_image":
        return str(args.get("image_path", ""))
    return ""


def describe_operation(name: str, args: dict) -> tuple[str, str]:
    """返回 `(一句话标题, 小白解释)`。"""
    path = str(args.get("path", ""))
    source = str(args.get("source", ""))
    destination = str(args.get("destination", ""))
    command = str(args.get("command", ""))
    mode = str(args.get("mode", "write"))

    if name == "computer_use":
        return _describe_computer_use(str(args.get("action", "")), args)

    if name == "get_system_info":
        return "查看电脑配置", "读取了系统版本、内存和磁盘占用，没有改动任何文件。"
    if name == "list_directory":
        return "查看文件夹内容", f"打开了「{path or '.'}」，看看里面有哪些文件和文件夹。"
    if name == "read_text_file":
        return "读取文件内容", f"打开了「{path}」并读取其中的文字，没有改动文件。"
    if name == "search_files":
        return (
            "按名字找文件",
            f"在「{args.get('root', '.')}」里查找名字匹配「{args.get('pattern', '*')}」的文件。",
        )
    if name == "search_text":
        return (
            "按内容找文件",
            f"在「{args.get('root', '.')}」里查找包含「{args.get('keyword', '')}」的文字。",
        )
    if name == "list_processes":
        return "查看运行中的程序", "读取了当前占用内存最多的程序列表，没有改动任何东西。"
    if name == "write_text_file":
        action = "追加写入" if mode == "append" else "写入（会覆盖原内容）"
        return "写入文件", f"{action}「{path}」。"
    if name == "create_directory":
        return "创建文件夹", f"新建文件夹「{path}」。"
    if name == "move_path":
        return "移动或重命名", f"把「{source}」移动到「{destination}」。原位置将不再保留。"
    if name == "copy_path":
        return "复制文件", f"把「{source}」复制一份到「{destination}」。原文件仍然保留。"
    if name == "delete_path":
        if args.get("permanent"):
            return "永久删除", f"把「{path}」从电脑上彻底删除。"
        return "移到回收区", f"把「{path}」移到工作区的 .agent_trash 回收目录。"
    if name == "run_command":
        return "执行系统命令", f"运行命令「{command}」。"
    if name == "open_path":
        return "打开文件或网址", f"用系统默认程序打开「{path}」。"
    return name, "执行了一个电脑操作。"


def operation_risk(name: str, args: dict) -> str:
    """返回这个操作的风险说明。"""
    if name == "computer_use":
        return _computer_use_risk(str(args.get("action", "")))
    if name == "get_system_info":
        return (
            "会读取系统版本、主机名、当前用户名、内存和磁盘信息。"
            "这些信息只用于回答，不会上传；但仍可能包含设备标识。"
        )
    if name == "list_processes":
        return (
            "会读取当前运行的程序列表和所属用户。"
            "这些信息只用于回答，不会上传；但可能暴露你正在使用哪些软件。"
        )
    if name == "write_text_file":
        if str(args.get("mode", "write")) == "append":
            return "会在文件末尾增加内容，不会删除原有内容。"
        return "如果文件已存在，原内容会被替换。系统会先自动备份原文件。"
    if name == "create_directory":
        return "只是新建文件夹，不会删除或修改已有文件。"
    if name == "move_path":
        return "原位置的文件会消失，移动到新位置。请确认目标路径正确。"
    if name == "copy_path":
        return "原文件保留，只是多复制一份。如果目标已有同名文件，可能被覆盖。"
    if name == "delete_path":
        if args.get("permanent"):
            return "永久删除不可恢复，文件不会进入回收区。"
        return "文件会移到工作区的 .agent_trash 回收目录，可以手动恢复。"
    if name == "run_command":
        return (
            "命令会以你当前的用户权限执行，可能修改文件、网络或系统设置。"
            "请确认你理解这条命令的作用，不要批准来路不明的命令。"
        )
    if name == "open_path":
        return "会启动系统默认程序。如果打开的是可执行文件，可能会运行它。"
    return "该操作会修改电脑状态，请确认后再执行。"


def operation_target(name: str, args: dict) -> str:
    """返回确认卡片上要展示的目标，便于用户核对。"""
    if name == "computer_use":
        return _computer_use_target(str(args.get("action", "")), args)
    if name in {"write_text_file", "create_directory", "delete_path", "read_text_file"}:
        return str(args.get("path", ""))
    if name in {"move_path", "copy_path"}:
        return f"{args.get('source', '')}  ->  {args.get('destination', '')}"
    if name in {"list_directory", "search_files", "search_text"}:
        return str(args.get("root", args.get("path", "")))
    if name == "run_command":
        cwd = args.get("cwd")
        return f"{args.get('command', '')}" + (f"（工作目录：{cwd}）" if cwd else "")
    if name == "open_path":
        return str(args.get("path", ""))
    return ""


def measure_path(path: Path, max_items: int = 2000) -> str:
    """统计文件/目录的影响范围，用于删除前提示。"""
    try:
        if path.is_file():
            return f"1 个文件，共 {format_size(path.stat().st_size)}"
        count = 0
        total = 0
        for current_root, _dirs, files in os.walk(path):
            for name in files:
                count += 1
                try:
                    total += (Path(current_root) / name).stat().st_size
                except OSError:
                    continue
                if count >= max_items:
                    return f"至少 {count} 个文件，共 {format_size(total)}（扫描已达上限）"
        return f"{count} 个文件，共 {format_size(total)}"
    except OSError:
        return "无法统计（可能被占用或没有权限）"


def summarize_result(result: str, limit: int = 160) -> str:
    """把工具结果压缩成一行，用于操作记录。"""
    text = str(result or "").strip()
    if not text:
        return "没有返回内容"
    first = text.splitlines()[0].strip()
    if len(first) > limit:
        first = first[:limit] + "…"
    return first


def sanitize_tool_args(tool_name: str, args: dict) -> dict:
    """日志/审计用的参数脱敏。

    目前只针对 computer_use 的文字输入与剪贴板写入：这些内容可能是密码、验证码或
    密钥，不应原样落盘。工具本身仍然收到完整参数。
    """
    if tool_name != "computer_use" or not isinstance(args, dict):
        return args
    action = str(args.get("action", ""))
    if action in {"type_text", "clipboard_set"}:
        text = str(args.get("text", ""))
        return {**args, "text": f"***（已脱敏，{len(text)} 字符）"}
    return args


def sanitize_tool_result(tool_name: str, args: dict, content: object) -> str:
    """日志/审计用的结果脱敏；图片 base64 与剪贴板正文不落盘。"""
    text = summarize_content_for_log(content)
    if (
        tool_name == "computer_use"
        and isinstance(args, dict)
        and args.get("action") == "clipboard_get"
    ):
        return f"***（剪贴板内容已脱敏，{len(text)} 字符）"
    return text


def summarize_content_for_log(content: object, limit: int = 2000) -> str:
    """把工具结果转成可读文本，供操作记录与审计日志使用。

    工具可能返回 LangChain content block 列表；截图会带一大段 base64，直接 str()
    会把日志和界面撑爆，因此这里把图片块替换成占位说明，只保留文字部分。
    """
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                block_type = str(block.get("type", ""))
                if block_type == "text":
                    parts.append(str(block.get("text", "")))
                elif block_type in {"image", "image_url"}:
                    parts.append("[图片已附加给模型，base64 已省略]")
                elif block_type in {"file", "document"}:
                    parts.append(f"[{block_type} 内容已省略]")
                else:
                    parts.append(f"[{block_type or '未知内容块'}]")
            else:
                parts.append(str(block))
        text = "\n".join(part for part in parts if part)
    else:
        text = str(content or "")

    text = text.strip()
    if len(text) > limit:
        text = text[:limit] + f"…（已截断，共 {len(text)} 字符）"
    return text


def format_size(size: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{size}B"
