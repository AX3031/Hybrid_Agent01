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

from utils.config_handler import agent_conf
from utils.path_tool import get_project_root

# ---------------------------------------------------------------------------
# 路径展开与工作区域
# ---------------------------------------------------------------------------


def expand_path(raw: str) -> Path:
    """展开 ~、环境变量和包裹引号，返回绝对路径。"""
    text = str(raw).strip().strip('"').strip("'")
    text = os.path.expandvars(os.path.expanduser(text))
    return Path(text or ".").resolve()


def _norm(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _is_under(path: Path, root: Path) -> bool:
    """大小写不敏感的前缀判断，兼容 Windows 路径。"""
    child, parent = _norm(path), _norm(root)
    return child == parent or child.startswith(parent + os.sep)


def _load_workspace_roots() -> list[Path]:
    """读取工作区域配置；为空时默认「用户目录 + 项目根目录」。"""
    configured = agent_conf.get("workspace_roots") or ["~", "."]
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


def workspace_roots() -> list[Path]:
    """返回当前生效的工作区域列表。"""
    return list(WORKSPACE_ROOTS)


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


def is_sensitive(path: Path) -> bool:
    """判断路径是否经过敏感目录（如 .ssh、.aws）。"""
    parts = [part.lower() for part in path.parts]
    return any(name in parts for name in _SENSITIVE_NAMES)


def is_system_protected(path: Path) -> bool:
    """判断路径是否属于系统保护目录或盘符根目录。"""
    if len(path.parts) <= 1:
        return True
    return any(_is_under(path, root) for root in _PROTECTED_ROOTS)


def guard_path(raw: str, *, for_write: bool = False) -> tuple[Path | None, str]:
    """路径的统一安全入口。

    返回 `(path, "")` 表示通过；`(None, 错误说明)` 表示拒绝。
    """
    path = expand_path(raw)
    if not in_workspace(path):
        return None, (
            f"不在允许的工作区域内，已拒绝：{path}\n"
            f"当前工作区域：\n{workspace_text()}\n"
            "如需访问其他目录，请修改 config/agent.yml 的 workspace_roots 后重启应用。"
        )
    if is_sensitive(path):
        return None, f"该路径属于敏感目录，已拒绝访问：{path}"
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


def backup_before_overwrite(path: Path) -> str:
    """覆盖已有文件前先备份，返回一段给用户看的说明。"""
    if not path.exists() or not path.is_file():
        return ""
    try:
        if path.stat().st_size > _BACKUP_MAX_BYTES:
            return "（原文件超过 10MB，未自动备份）"
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
        return f"（原文件已备份到 {dest}）"
    except OSError as exc:
        return f"（原文件备份失败：{exc}）"


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
    (re.compile(r"\bdel\b[^\n]*\s/[sq]\b", re.IGNORECASE), "强制或静默删除"),
    (re.compile(r"\b(rd|rmdir)\b[^\n]*\s/s\b", re.IGNORECASE), "递归删除目录"),
    (re.compile(r"\btaskkill\b[^\n]*/f\b", re.IGNORECASE), "强制结束进程"),
    (re.compile(r"\bnet\s+user\b[^\n]*/add\b", re.IGNORECASE), "创建系统用户"),
    (re.compile(r"\bnetsh\b", re.IGNORECASE), "修改网络配置"),
    (re.compile(r"\bcipher\b[^\n]*/w\b", re.IGNORECASE), "擦除磁盘空闲空间"),
    (re.compile(r"\bvssadmin\b[^\n]*delete", re.IGNORECASE), "删除卷影副本"),
    (re.compile(r"\bschtasks\b[^\n]*/create\b", re.IGNORECASE), "创建计划任务"),
]

_WIN_ABS_PATH = re.compile(r"([A-Za-z]:\\[^\s\"'|<>]*)")


def check_command(command: str) -> str:
    """检查命令是否命中黑名单或引用工作区外路径；通过时返回空字符串。"""
    text = str(command).strip()
    if not text:
        return "命令不能为空"

    for pattern, label in _DANGEROUS_COMMANDS:
        if pattern.search(text):
            return f"该命令被安全策略拦截（识别为：{label}）：{text}"

    for match in _WIN_ABS_PATH.finditer(text):
        raw = match.group(1).rstrip("\\.,;")
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


def describe_operation(name: str, args: dict) -> tuple[str, str]:
    """返回 `(一句话标题, 小白解释)`。"""
    path = str(args.get("path", ""))
    source = str(args.get("source", ""))
    destination = str(args.get("destination", ""))
    command = str(args.get("command", ""))
    mode = str(args.get("mode", "write"))

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


def format_size(size: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{size}B"
