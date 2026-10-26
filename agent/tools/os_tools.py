"""电脑信息获取与文件操作工具。

安全约束：
- 所有路径操作（包括读取）都必须落在 `config/agent.yml` 的 `workspace_roots` 内；
- 敏感目录（`.ssh`、`.aws` 等）一律拒绝；
- 写文件拒绝可执行后缀，覆盖前自动备份；
- 删除默认移入 `.agent_trash` 回收区，只有 permanent=True 才真正删除；
- 命令执行会过危险命令黑名单和工作区外路径检查。

只读工具直接执行；有副作用的工具由 HumanInTheLoopMiddleware 在调用前拦截，
等待用户在界面上确认后才真正执行。
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

from agent.tools.safety import (
    backup_before_overwrite,
    check_command,
    check_write_suffix,
    format_size,
    guard_path,
    move_to_trash,
    workspace_roots,
)
from utils.logger_handler import logger

# 单次读取上限，避免误读超大文件把上下文撑爆
_MAX_READ_BYTES = 2 * 1024 * 1024
# 单次文本搜索扫描的文件数上限
_MAX_SEARCH_FILES = 5000
# 单次搜索跳过的文件大小上限
_MAX_SEARCH_FILE_BYTES = 5 * 1024 * 1024
# 返回给模型的文本长度上限
_MAX_RESULT_CHARS = 8000


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


def _format_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


@tool(description="获取当前电脑的系统信息：操作系统、主机名、当前用户、CPU 核心数、内存、磁盘占用、工作区域，以及桌面/下载/文档目录。无入参。")
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
        f"内存：已用 {format_size(memory.used)} / 共 {format_size(memory.total)}（{memory.percent}%）",
    ]

    for name in ("Desktop", "Downloads", "Documents"):
        candidate = home / name
        if candidate.exists():
            lines.append(f"{name} 目录：{candidate}")

    lines.append("工作区域（文件操作只能在这些目录内）：")
    for root in workspace_roots():
        lines.append(f"  {root}")

    lines.append("磁盘：")
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        lines.append(
            f"  {part.device} 已用 {format_size(usage.used)} / 共 {format_size(usage.total)}"
            f"（{usage.percent}%）挂载点 {part.mountpoint}"
        )

    return "\n".join(lines)


@tool(description="列出工作区域内指定目录下的文件和子目录，返回名称、类型、大小和修改时间。path 可省略，默认为项目根目录；show_hidden 控制是否显示以点开头的隐藏项。")
def list_directory(path: str = ".", show_hidden: bool = False) -> str:
    target, error = guard_path(path)
    if error:
        return error
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
        size = "-" if is_dir else format_size(stat.st_size)
        lines.append(f"[{kind}] {name} | {size} | {_format_time(stat.st_mtime)}")

    if len(entries) > 500:
        lines.append(f"...（仅显示前 500 项，共 {len(entries)} 项）")
    return "\n".join(lines)


@tool(description="读取工作区域内文本文件的内容。path 为文件路径；start 为起始字符偏移，max_chars 为最多返回的字符数，用于分段读取大文件。")
def read_text_file(path: str, start: int = 0, max_chars: int = 20000) -> str:
    target, error = guard_path(path)
    if error:
        return error
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


@tool(description="按文件名模式在工作区域内搜索文件。root 为搜索根目录（必须在工作区域内），pattern 支持通配符（如 *.py、report*），max_results 为结果数量上限。")
def search_files(root: str = ".", pattern: str = "*", max_results: int = 50) -> str:
    base, error = guard_path(root)
    if error:
        return error
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


@tool(description="在工作区域内按文件内容搜索关键词。root 为搜索根目录（必须在工作区域内），keyword 为要查找的文本，file_glob 限定文件类型（如 *.py），max_results 为命中行数上限。")
def search_text(
    root: str = ".",
    keyword: str = "",
    file_glob: str = "*.*",
    max_results: int = 50,
) -> str:
    base, error = guard_path(root)
    if error:
        return error
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
        lines.append(f"PID {pid} | {name} | {format_size(rss)} | {username}")
    return "\n".join(lines)


@tool(description="把文本写入工作区域内的文件（有副作用，需用户确认）。path 为文件路径；mode 为 write（覆盖）或 append（追加）；覆盖前会自动备份原文件；拒绝写入 .exe/.bat/.ps1 等可执行后缀。")
def write_text_file(
    path: str,
    content: str,
    mode: Literal["write", "append"] = "write",
) -> str:
    target, error = guard_path(path, for_write=True)
    if error:
        return error

    suffix_error = check_write_suffix(target)
    if suffix_error:
        return suffix_error
    if target.exists() and target.is_dir():
        return f"目标是目录，不能写入文件：{target}"

    backup_note = backup_before_overwrite(target) if mode == "write" else ""

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a" if mode == "append" else "w", encoding="utf-8") as stream:
            stream.write(content)
    except OSError as exc:
        return f"写入失败：{target}，{exc}"

    logger.info(f"[write_text_file]已写入 {target}（mode={mode}）")
    action = "追加" if mode == "append" else "写入"
    return f"已{action} {len(content)} 个字符到 {target}{backup_note}"


@tool(description="在工作区域内创建目录（有副作用，需用户确认）。path 为目录路径，已存在时直接返回成功。")
def create_directory(path: str) -> str:
    target, error = guard_path(path, for_write=True)
    if error:
        return error

    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"创建目录失败：{target}，{exc}"
    return f"目录已就绪：{target}"


@tool(description="在工作区域内移动或重命名文件/目录（有副作用，需用户确认）。source 和 destination 都必须在工作区域内；目标为已存在目录时移动到该目录下。")
def move_path(source: str, destination: str) -> str:
    src, error = guard_path(source, for_write=True)
    if error:
        return error
    dst, error = guard_path(destination, for_write=True)
    if error:
        return error
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


@tool(description="在工作区域内复制文件或目录（有副作用，需用户确认）。source 必须在工作区域内，destination 也必须在工作区域内；目标为已存在目录时复制到该目录下。")
def copy_path(source: str, destination: str) -> str:
    src, error = guard_path(source)
    if error:
        return error
    dst, error = guard_path(destination, for_write=True)
    if error:
        return error
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


@tool(description="删除工作区域内的文件或目录（有副作用，需用户确认）。默认移到 .agent_trash 回收区，permanent=True 才永久删除；非空目录需要 recursive=True。")
def delete_path(path: str, recursive: bool = False, permanent: bool = False) -> str:
    target, error = guard_path(path, for_write=True)
    if error:
        return error
    if not target.exists():
        return f"路径不存在：{target}"

    # 空目录：直接删，不需要进回收区
    if target.is_dir() and not recursive:
        try:
            target.rmdir()
        except OSError as exc:
            return f"删除失败（目录可能非空，需要 recursive=True）：{target}，{exc}"
        logger.info(f"[delete_path]已删除空目录 {target}")
        return f"已删除空目录：{target}"

    if permanent:
        try:
            if target.is_dir():
                shutil.rmtree(str(target))
            else:
                target.unlink()
        except OSError as exc:
            return f"永久删除失败：{target}，{exc}"
        logger.info(f"[delete_path]已永久删除 {target}")
        return f"已永久删除：{target}（不可恢复）"

    dest, trash_error = move_to_trash(target)
    if trash_error:
        return trash_error
    logger.info(f"[delete_path]已移入回收区 {target} -> {dest}")
    return f"已移到回收区：{dest}（如需彻底删除，请显式使用 permanent=True）"


@tool(description="在工作区域内执行系统命令（有副作用，需用户确认）。command 为命令字符串，cwd 为工作目录（必须在工作区域内），timeout 为超时秒数；危险命令和工作区外路径会被拒绝。")
def run_command(command: str, cwd: str | None = None, timeout: int = 60) -> str:
    command_error = check_command(command)
    if command_error:
        return command_error

    workdir = None
    if cwd:
        workdir, error = guard_path(cwd)
        if error:
            return error
        if not workdir.is_dir():
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


@tool(description="用系统默认程序打开工作区域内的文件/目录，或打开网址（有副作用，需用户确认）。path 为文件路径、目录路径或 http(s) URL。")
def open_path(path: str) -> str:
    text = str(path).strip().strip('"').strip("'")
    if text.startswith(("http://", "https://")):
        target = text
    else:
        resolved, error = guard_path(text)
        if error:
            return error
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
