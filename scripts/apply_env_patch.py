"""把 env_patch/ 的运行时补丁自动装进当前虚拟环境。

背景：`uv sync` 每次重建 `.venv` 都会清空 site-packages，README 4.2 要求手工复制
的 `*.pth` 与 `sitecustomize.py` 因此会丢失，表现为 IDE 里直接运行 `.py` 报
`ModuleNotFoundError`、Windows 终端中文/emoji 编码错误。本脚本把那一步变成一条
可重复执行的命令。

用法：
    uv run python scripts/apply_env_patch.py             # 应用（幂等）
    uv run python scripts/apply_env_patch.py --check     # 只检查，未生效则退出码 1
    uv run python scripts/apply_env_patch.py --verbose   # 打印每个文件的状态
"""

from __future__ import annotations

import argparse
import sys
import sysconfig
from pathlib import Path

# env_patch 下需要装进 site-packages 的文件类型
PATCH_SUFFIXES = {".pth", ".py"}


def project_root() -> Path:
    """脚本位于 <root>/scripts/，上一级即项目根。"""
    return Path(__file__).resolve().parents[1]


def site_packages() -> Path:
    """当前解释器的 site-packages；由解释器自报，不必写死 Windows 的 Lib 目录。"""
    return Path(sysconfig.get_paths()["purelib"])


def patch_files(patch_dir: Path) -> list[Path]:
    """列出待安装的补丁文件（按文件名通配，env_patch 改名也不用改这里）。"""
    if not patch_dir.is_dir():
        raise SystemExit(f"找不到补丁目录：{patch_dir}")
    files = sorted(
        p for p in patch_dir.iterdir() if p.is_file() and p.suffix in PATCH_SUFFIXES
    )
    if not files:
        raise SystemExit(f"补丁目录里没有 .pth / .py 文件：{patch_dir}")
    return files


def render_pth(content: str, dest: Path) -> str:
    """把 .pth 里相对 site-packages 的路径行改写成绝对路径。

    源文件里的 `../../..` 是按 Windows 的 `.venv/Lib/site-packages` 层数写的，
    Linux/macOS 是 `.venv/lib/python3.x/site-packages`，层数不同会指错目录，
    因此统一解析成绝对路径。注释行与 `import ...` 行按 .pth 规范原样保留。
    """
    lines = []
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("import "):
            lines.append(line)
            continue
        resolved = (dest / line).resolve()
        lines.append(str(resolved) if resolved.exists() else line)
    return "\n".join(lines) + "\n"


def expected_files(patch_dir: Path, dest: Path) -> list[tuple[str, str]]:
    """返回 (目标文件名, 期望内容) 列表。"""
    wanted = []
    for src in patch_files(patch_dir):
        text = src.read_text(encoding="utf-8")
        wanted.append((src.name, render_pth(text, dest) if src.suffix == ".pth" else text))
    return wanted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把 env_patch 的 .pth / sitecustomize.py 装进虚拟环境的 site-packages"
    )
    parser.add_argument("--check", action="store_true", help="只检查是否已生效，不写入文件")
    parser.add_argument("--verbose", action="store_true", help="打印每个文件的处理状态")
    args = parser.parse_args(argv)

    root = project_root()
    dest = site_packages()

    if sys.prefix == sys.base_prefix:
        print(
            "当前 Python 不是虚拟环境的解释器，请用：uv run python scripts/apply_env_patch.py",
            file=sys.stderr,
        )
        return 2
    if not dest.is_dir():
        print(f"虚拟环境目录不存在：{dest}\n请先执行：uv sync", file=sys.stderr)
        return 2

    pending: list[tuple[Path, str]] = []
    for name, expected in expected_files(root / "env_patch", dest):
        target = dest / name
        if not target.exists():
            state = "缺失"
        elif target.read_text(encoding="utf-8") != expected:
            state = "过期"
        else:
            state = "已最新"
        if args.verbose or state != "已最新":
            print(f"[{state}] {name}")
        if state != "已最新":
            pending.append((target, expected))

    if args.check:
        if pending:
            print(f"补丁未生效（{len(pending)} 个待写入），执行：uv run python scripts/apply_env_patch.py")
            return 1
        print(f"补丁已生效：{dest}")
        return 0

    if not pending:
        print(f"补丁已是最新，无需改动：{dest}")
        return 0

    for target, expected in pending:
        target.write_text(expected, encoding="utf-8")
        print(f"已写入 {target.name}")
    print(f"完成，共写入 {len(pending)} 个文件到 {dest}")
    print("提示：sitecustomize 只在新的 Python 进程里生效，请重开终端或重启 Streamlit。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
