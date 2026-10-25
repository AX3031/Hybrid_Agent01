"""环境级运行时补丁（只作用于当前 .venv，不修改任何项目源码）。

1. Windows 终端下把输出切换为 UTF-8，避免中文乱码；
2. 输出被管道/IDE 捕获时保留原编码，仅把无法编码的字符替换为 '?'，
   避免模型输出 emoji 时抛 UnicodeEncodeError。
"""
import os
import sys


def _is_tty(stream):
    try:
        return bool(stream and stream.isatty())
    except Exception:
        return False


def _reconfigure(**kwargs):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(**kwargs)
        except (AttributeError, ValueError, OSError):
            pass


_is_console = _is_tty(sys.stdout) or _is_tty(sys.stderr)

if os.name == "nt" and _is_console:
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleCP(65001)
    except Exception:
        pass

if os.name != "nt" or _is_console:
    _reconfigure(encoding="utf-8", errors="replace")
else:
    _reconfigure(errors="replace")
