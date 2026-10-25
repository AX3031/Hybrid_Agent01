"""电脑信息获取与文件操作工具。

只读工具（系统信息、列目录、读文件、搜索、进程）直接执行；
有副作用的工具（写文件、移动、复制、删除、执行命令、打开路径）由
HumanInTheLoopMiddleware 在调用前拦截，等待用户确认后才真正执行。
"""

import fnmatch
import getpass
import os
import platform
import shutil
import socket
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Literal

import psutil
from langchain_core.tools import tool

from utils.logger_handler import logger

# 单次读取上限，避免误读超大文件把上下文撑爆
_MAX_READ_BYTES = 2 * 1024 * 1024
# 单次文本搜索扫描的文件数上限
_MAX_SEARCH_FILES = 5000
# 单次搜索跳过的文件大小上限
_MAX_SEARCH_FILE_BYTES = 5 * 1024 * 1024
# 返回给模型的文本长度上限
_MAX_RESULT_CHARS = 8000

# 写/删类操作的系统保护目录，拒绝直接修改
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


def _resolve(raw: str) -> Path:
    """把用户给的路径展开为绝对路径，去掉可能包裹的引号。"""
    text = str(raw).strip().strip('"').strip("'")
    text = os.path.expandvars(os.path.expanduser(text))
    return Path(text).resolve()


def _is_protected(path: Path) -> bool:
    """判断路径是否落在系统保护目录内。"""
    if len(path.parts) <= 1:  # 盘符根目录
        return True
    for root in _PROTECTED_ROOTS:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _truncate(text: str, limit: int = _MAX_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...（内容过长，已截断，共 {len(text)} 字符）"


def _decode(raw: bytes) -> str:
    """Windows 下命令输出可能是 GBK，按常见编码依次尝试。"""
    for encoding in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _read_text(path: Path) -> str:
    raw = path.read_bytes()[:_MAX_READ_BYTES]
    return _decode(raw)


def _format_size(size: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{size}B"


def _format_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


@tool(description="获取当前电脑的系统信息：操作系统、主机名、当前用户、CPU 核心数、内存、磁盘占用，以及桌面/下载/文档目录的绝对路径。无入参。")
def get_system_info() -> str:
    home = Path.home()
    memory = psutil.virtual_memory()
    lines = [
        f"操作系统：{platform.system()} {platform.release()}（{platform.machine()}）",
        f"系统版本：{platform.version()}",
        f"主机名：{socket.gethostname()}",
        f"当前用户：{getpass.getuser()}",
        f"用户目录：{home}",
        f"当前工作目录：{Path.cwd()}",
        f"CPU 核心数：{os.cpu_count()}",
        f"内存：已用 {_format_size(memory.used)} / 共 {_format_size(memory.total)}（{memory.percent}%）",
    ]

    for name in ("Desktop", "Downloads", "Documents"):
        candidate = home / name
        if candidate.exists():
            lines.append(f"{name} 目录：{candidate}")

    lines.append("磁盘：")
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        lines.append(
            f"  {part.device} 已用 {_format_size(usage.used)} / 共 {_format_size(usage.total)}"
            f"（{usage.percent}%）挂载点 {part.mountpoint}"
        )

    return "\n".join(lines)


@tool(description="列出指定目录下的文件和子目录，返回名称、类型、大小和修改时间。path 可省略，默认为当前工作目录；show_hidden 控制是否显示隐藏文件。")
def list_directory(path: str = ".", show_hidden: bool = False) -> str:
    target = _resolve(path)
    if not target.exists():
        return f"目录不存在：{target}"
    if not target.is_dir():
        return f"不是目录：{target}"

    entries = []
    try:
        for item in target.iterdir():
            if not show_hidden and item.name.startswith("."):
                continue
            try:
                stat = item.stat()
            except OSError:
                continue
            kind = "目录" if item.is_dir() else "文件"
            entries.append((item.is_dir(), item.name, kind, stat))
    except PermissionError:
        return f"没有权限读取目录：{target}"

    entries.sort(key=lambda row: (not row[0], row[1].lower()))
    lines = [f"目录：{target}（共 {len(entries)} 项）"]
    for is_dir, name, kind, stat in entries[:500]:
        size = "-" if is_dir else _format_size(stat.st_size)
        lines.append(f"[{kind}] {name} | {size} | {_format_time(stat.st_mtime)}")

    if len(entries) > 500:
        lines.append(f"...（仅显示前 500 项，共 {len(entries)} 项）")
    return "\n".join(lines)


@tool(description="读取文本文件内容。path 为文件绝对路径；start 为起始字符偏移，max_chars 为最多返回的字符数，用于分段读取大文件。")
def read_text_file(path: str, start: int = 0, max_chars: int = 20000) -> str:
    target = _resolve(path)
    if not target.exists():
        return f"文件不存在：{target}"
    if not target.is_file():
        return f"不是文件：{target}"

    try:
        content = _read_text(target)
    except PermissionError:
        return f"没有权限读取文件：{target}"
    except OSError as exc:
        return f"读取文件失败：{target}，{exc}"

    if "\x00" in content[:1000]:
        return f"这是二进制文件，无法按文本读取：{target}"

    start = max(0, start)
    max_chars = max(1, min(max_chars, 50000))
    chunk = content[start : start + max_chars]
    header = f"文件：{target}（总长度 {len(content)} 字符，本次返回 {start}-{start + len(chunk)}）"
    return f"{header}\n{chunk}"


@tool(description="按文件名模式搜索文件。root 为搜索根目录，pattern 支持通配符（如 *.py、report*、*.docx），max_results 为结果数量上限。")
def search_files(root: str = ".", pattern: str = "*", max_results: int = 50) -> str:
    base = _resolve(root)
    if not base.is_dir():
        return f"不是目录：{base}"

    keyword = pattern.strip() or "*"
    if not any(ch in keyword for ch in "*?["):
        keyword = f"*{keyword}*"

    max_results = max(1, min(max_results, 200))
    matches: list[Path] = []
    for current_root, dir_names, file_names in os.walk(base):
        dir_names[:] = [d for d in dir_names if not d.startswith(".")]
        for name in file_names:
            if fnmatch.fnmatch(name.lower(), keyword.lower()):
                matches.append(Path(current_root) / name)
                if len(matches) >= max_results:
                    break
        if len(matches) >= max_results:
            break

    if not matches:
        return f"未找到匹配 {keyword} 的文件（搜索根目录：{base}）"

    lines = [f"在 {base} 下找到 {len(matches)} 个匹配 {keyword} 的文件："]
    lines.extend(str(path) for path in matches)
    return "\n".join(lines)


@tool(description="在文件内容中搜索关键词。root 为搜索根目录，keyword 为要查找的文本，file_glob 限定文件类型（如 *.py、*.txt、*.log），max_results 为命中行数上限。")
def search_text(
    root: str = ".",
    keyword: str = "",
    file_glob: str = "*.*",
    max_results: int = 50,
) -> str:
    base = _resolve(root)
    if not base.is_dir():
        return f"不是目录：{base}"
    if not keyword.strip():
        return "keyword 不能为空"

    needle = keyword.strip()
    max_results = max(1, min(max_results, 200))
    hits: list[str] = []
    scanned = 0

    for current_root, dir_names, file_names in os.walk(base):
        dir_names[:] = [d for d in dir_names if not d.startswith(".")]
        for name in file_names:
            if not fnmatch.fnmatch(name.lower(), file_glob.lower()):
                continue
            path = Path(current_root) / name
            try:
                if path.stat().st_size > _MAX_SEARCH_FILE_BYTES:
                    continue
                lines = _read_text(path).splitlines()
            except (OSError, PermissionError):
                continue

            scanned += 1
            for line_no, line in enumerate(lines, start=1):
                if needle.lower() in line.lower():
                    hits.append(f"{path}:{line_no}: {line.strip()[:300]}")
                    if len(hits) >= max_results:
                        break
            if len(hits) >= max_results or scanned >= _MAX_SEARCH_FILES:
                break
        if len(hits) >= max_results or scanned >= _MAX_SEARCH_FILES:
            break

    if not hits:
        return f"在 {base} 的 {file_glob} 文件中未找到「{needle}」（已扫描 {scanned} 个文件）"
    return f"在 {base} 下命中 {len(hits)} 处（已扫描 {scanned} 个文件）：\n" + "\n".join(hits)


@tool(description="列出当前占用内存最多的进程，返回 PID、进程名、内存占用和所属用户。top_n 为返回数量，默认 20。")
def list_processes(top_n: int = 20) -> str:
    top_n = max(1, min(top_n, 100))
    rows = []
    for proc in psutil.process_iter(["pid", "name", "username", "memory_info"]):
        try:
            info = proc.info
            memory = info.get("memory_info")
            rows.append(
                (
                    memory.rss if memory else 0,
                    info.get("pid"),
                    info.get("name") or "unknown",
                    info.get("username") or "",
                )
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    rows.sort(reverse=True)
    lines = [f"内存占用最高的 {min(top_n, len(rows))} 个进程："]
    for rss, pid, name, username in rows[:top_n]:
        lines.append(f"PID {pid} | {name} | {_format_size(rss)} | {username}")
    return "\n".join(lines)


@tool(description="把文本写入文件（有副作用，需用户确认）。path 为文件绝对路径；mode 为 write（覆盖）或 append（追加）；父目录不存在时会自动创建。")
def write_text_file(
    path: str,
    content: str,
    mode: Literal["write", "append"] = "write",
) -> str:
    target = _resolve(path)
    if _is_protected(target):
        return f"拒绝写入系统保护目录：{target}"

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a" if mode == "append" else "w", encoding="utf-8") as stream:
            stream.write(content)
    except OSError as exc:
        return f"写入失败：{target}，{exc}"

    logger.info(f"[write_text_file]已写入 {target}（mode={mode}）")
    return f"已{'追加' if mode == 'append' else '写入'} {len(content)} 个字符到 {target}"


@tool(description="创建目录（有副作用，需用户确认）。path 为目录绝对路径，已存在时直接返回成功。")
def create_directory(path: str) -> str:
    target = _resolve(path)
    if _is_protected(target):
        return f"拒绝在系统保护目录创建：{target}"

    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"创建目录失败：{target}，{exc}"
    return f"目录已就绪：{target}"


@tool(description="移动或重命名文件/目录（有副作用，需用户确认）。source 为源路径，destination 为目标路径；目标为已存在目录时移动到该目录下。")
def move_path(source: str, destination: str) -> str:
    src = _resolve(source)
    dst = _resolve(destination)
    if _is_protected(src) or _is_protected(dst):
        return "拒绝操作系统保护目录"
    if not src.exists():
        return f"源路径不存在：{src}"
    if dst.is_dir():
        dst = dst / src.name

    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
    except OSError as exc:
        return f"移动失败：{src} -> {dst}，{exc}"

    logger.info(f"[move_path]已移动 {src} -> {dst}")
    return f"已移动：{src} -> {dst}"


@tool(description="复制文件或目录（有副作用，需用户确认）。source 为源路径，destination 为目标路径；目标为已存在目录时复制到该目录下。")
def copy_path(source: str, destination: str) -> str:
    src = _resolve(source)
    dst = _resolve(destination)
    if _is_protected(dst):
        return f"拒绝复制到系统保护目录：{dst}"
    if not src.exists():
        return f"源路径不存在：{src}"
    if dst.is_dir():
        dst = dst / src.name

    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(str(src), str(dst), dirs_exist_ok=True)
        else:
            shutil.copy2(str(src), str(dst))
    except OSError as exc:
        return f"复制失败：{src} -> {dst}，{exc}"

    logger.info(f"[copy_path]已复制 {src} -> {dst}")
    return f"已复制：{src} -> {dst}"


@tool(description="删除文件或目录（有副作用，需用户确认）。path 为目标路径；recursive 为 True 时递归删除整个目录，为 False 时只能删除空目录。")
def delete_path(path: str, recursive: bool = False) -> str:
    target = _resolve(path)
    if _is_protected(target):
        return f"拒绝删除系统保护目录：{target}"
    if not target.exists():
        return f"路径不存在：{target}"

    try:
        if target.is_dir():
            if recursive:
                shutil.rmtree(str(target))
            else:
                target.rmdir()
        else:
            target.unlink()
    except OSError as exc:
        return f"删除失败：{target}，{exc}"

    logger.info(f"[delete_path]已删除 {target}（recursive={recursive}）")
    return f"已删除：{target}"


@tool(description="执行系统命令（有副作用，需用户确认）。command 为要执行的命令字符串，cwd 为工作目录（可省略），timeout 为超时秒数。返回退出码、标准输出和标准错误。")
def run_command(command: str, cwd: str | None = None, timeout: int = 60) -> str:
    workdir = _resolve(cwd) if cwd else None
    if workdir is not None and not workdir.is_dir():
        return f"工作目录不存在：{workdir}"

    timeout = max(1, min(timeout, 600))
    try:
        completed = subprocess.run(
            command,
            shell=True,
            cwd=str(workdir) if workdir else None,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"命令超时（{timeout} 秒）：{command}"
    except OSError as exc:
        return f"命令执行失败：{command}，{exc}"

    stdout = _decode(completed.stdout).strip()
    stderr = _decode(completed.stderr).strip()
    logger.info(f"[run_command]执行 {command}，退出码 {completed.returncode}")

    parts = [f"命令：{command}", f"退出码：{completed.returncode}"]
    parts.append(f"标准输出：\n{stdout}" if stdout else "标准输出：（空）")
    if stderr:
        parts.append(f"标准错误：\n{stderr}")
    return _truncate("\n".join(parts))


@tool(description="用系统默认程序打开文件、目录或网址（有副作用，需用户确认）。path 为文件/目录绝对路径或 URL。")
def open_path(path: str) -> str:
    text = str(path).strip().strip('"').strip("'")
    if text.startswith(("http://", "https://")):
        target = text
    else:
        resolved = _resolve(text)
        if not resolved.exists():
            return f"路径不存在：{resolved}"
        target = str(resolved)

    try:
        if platform.system() == "Windows":
            os.startfile(target)  # type: ignore[attr-defined]
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["xdg-open", target])
    except OSError as exc:
        return f"打开失败：{target}，{exc}"

    logger.info(f"[open_path]已打开 {target}")
    return f"已用默认程序打开：{target}"


if __name__ == "__main__":
    print(get_system_info())
    print("-" * 40)
    print(list_directory("."))
