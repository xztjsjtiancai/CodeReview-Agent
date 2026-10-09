"""打包脚本：生成可提交的 zip。

排除规则与 .gitignore 保持一致，且额外做两项安全检查：

1. **拒绝打包 `.env`** —— 里面有真实 API Key，绝不能进提交物；
2. 拒绝打包虚拟环境、缓存、离线依赖目录（vendor 体积大且非源码）。

用法：
    python tools_dev/make_package.py --name 学号姓名
    python tools_dev/make_package.py --name 10086张三 --check    # 只检查不打包
"""

from __future__ import annotations

import argparse
from pathlib import Path
import zipfile


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT.parent

# 需要排除的目录名（任意层级出现即排除）
EXCLUDE_DIRS = {
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".pytest_tmp",
    ".ruff_cache",
    ".tmp",
    ".git",
    ".idea",
    ".vscode",
    "vendor",
    "node_modules",
    "dist",
    "build",
}

# 需要排除的文件名或后缀
# 另外排除任何本机专用的启动脚本：它们会硬编码含本机用户名的绝对路径，
# 属于个人机器信息，不应出现在提交物中。交付物的标准入口是 run.bat。
EXCLUDE_FILES = {".env"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".zip", ".rar", ".7z", ".log"}

# 体积上限（作业要求小于 200M）
SIZE_LIMIT_BYTES = 200 * 1024 * 1024


# 允许打包的启动脚本。其余 .bat 一律排除 —— 那些是为本机准备的
# （例如 run-local.bat 里写死了含 Windows 用户名的绝对路径）。
KEEP_SCRIPTS = {"run.bat", "xztcode.bat", "install.bat"}


def should_exclude(path: Path) -> bool:
    relative = path.relative_to(PROJECT_ROOT)
    if any(part in EXCLUDE_DIRS for part in relative.parts):
        return True
    if path.name in EXCLUDE_FILES:
        return True
    if path.suffix.lower() == ".bat" and path.name not in KEEP_SCRIPTS:
        return True
    return path.suffix.lower() in EXCLUDE_SUFFIXES


def collect_files() -> tuple[list[Path], list[Path]]:
    """返回 (要打包的文件, 被排除的文件)。"""
    included: list[Path] = []
    excluded: list[Path] = []
    for path in sorted(PROJECT_ROOT.rglob("*")):
        if not path.is_file():
            continue
        if should_exclude(path):
            excluded.append(path)
        else:
            included.append(path)
    return included, excluded


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True, help="压缩包名，通常是「学号姓名」")
    parser.add_argument("--check", action="store_true", help="只做检查，不生成压缩包")
    args = parser.parse_args()

    included, excluded = collect_files()
    total_bytes = sum(path.stat().st_size for path in included)

    print("== 打包内容 ==")
    for path in included:
        relative = path.relative_to(PROJECT_ROOT)
        print(f"  {path.stat().st_size:>9,}  {relative}")
    print(f"\n共 {len(included)} 个文件，{total_bytes / 1024:.1f} KB")

    print("\n== 已排除（按规则） ==")
    for path in excluded[:15]:
        print(f"  - {path.relative_to(PROJECT_ROOT)}")
    if len(excluded) > 15:
        print(f"  … 另有 {len(excluded) - 15} 项")

    # 安全检查 1：.env 必须被排除，绝不能进入打包内容
    leaked_env = [path for path in included if path.name == ".env"]
    if leaked_env:
        print(f"\n[错误] .env 会进入打包内容：{leaked_env}")
        print("       里面有真实 API Key，已中止。请检查 should_exclude 规则。")
        return 2
    excluded_env = [path for path in excluded if path.name == ".env"]
    print(f"\n[通过] .env 已排除（{len(excluded_env)} 个文件未进入打包内容，不含 API Key）")

    # 安全检查 2：不能包含任何 zip/压缩包，避免把上次的提交物打进去
    nested = [path for path in included if path.suffix.lower() in {".zip", ".rar", ".7z"}]
    if nested:
        print(f"[错误] 打包内容里仍有压缩包：{nested}")
        return 2
    print("[通过] 未包含嵌套压缩包")

    # 安全检查 3：体积
    if total_bytes > SIZE_LIMIT_BYTES:
        print(f"[错误] 体积 {total_bytes / 1024 / 1024:.1f} MB 超过 200 MB 上限。")
        return 2
    print(f"[通过] 体积 {total_bytes / 1024:.1f} KB，远小于 200 MB 上限")

    if args.check:
        print("\n（--check 模式：未生成压缩包）")
        return 0

    output = DEFAULT_OUTPUT_DIR / f"{args.name}.zip"
    if output.exists():
        output.unlink()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in included:
            archive.write(path, Path(PROJECT_ROOT.name) / path.relative_to(PROJECT_ROOT))

    size = output.stat().st_size
    print(f"\n已生成：{output}")
    print(f"压缩后体积：{size / 1024:.1f} KB")
    print("\n提示：压缩包外层目录为项目名，解压后直接得到完整源码树。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
