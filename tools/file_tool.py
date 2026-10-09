"""读文件、列目录。

这是让 Agent 真正能「碰外部世界」的两个工具,不然它只是个会说话的搜索框。

三条不能省的处理:

  * 返回的内容自带行号。模型引用行号时准确率会明显提高,不然它经常报
    「大概第 8 行」这种没法用的定位。
  * 编码按 utf-8 → gbk → 兜底替换 的顺序试。中文 Windows 上的源码
    真有不少是 GBK 的,直接按 utf-8 读会当场炸掉。
  * 路径必须落在工作目录里面。模型可能被用户的话带偏(「读一下
    ../../.env」),也可能只是写错,所以这里必须拦。

还有一点容易踩:`config_module.CONFIG` 是运行时读的,不是导入时绑定的。
写成 `from config import CONFIG` 的话,测试里把工作目录换掉之后,
工具还是拿着老的那份,路径校验就会按错误的地方判。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent.errors import ToolError
import config as config_module
from tools.base import Tool


# 一次最多吐这么多字符,再多就是拿上下文换钱了
RETURN_LIMIT = 40000

# 读文件时按顺序尝试的编码
ENCODING_CHAIN = ("utf-8", "gbk")

# 这些后缀一律不按文本处理
BINARY_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".ico",
    ".webp",
    ".pdf",
    ".zip",
    ".rar",
    ".7z",
    ".gz",
    ".tar",
    ".exe",
    ".dll",
    ".so",
    ".pyd",
    ".pyc",
    ".class",
    ".jar",
    ".mp3",
    ".mp4",
    ".avi",
    ".mov",
    ".woff",
    ".woff2",
}

# 行号前缀的格式:右对齐的行号 + 竖线 + 原文
LINE_FORMAT = "{number:>{width}} | {text}"


# ======================================================================
# 路径与文本处理
# ======================================================================


def workspace_root() -> Path:
    """当前生效的工作目录。每次调用都重新取,别缓存。"""
    return config_module.CONFIG.workspace_root.resolve()


def resolve_safe(path_str: str, workspace: Path | None = None) -> Path:
    """把路径变成绝对路径,并确认它没跑到工作目录外面去。"""
    root = (workspace or workspace_root()).resolve()

    candidate = Path(path_str)
    if not candidate.is_absolute():
        candidate = root / candidate

    resolved = candidate.resolve()

    if resolved == root:
        return resolved

    if root in resolved.parents:
        return resolved

    raise ToolError(
        f"拒绝访问工作目录之外的路径:{path_str}",
        f"当前工作目录是 {root},只能读它内部的东西",
    )


def read_text_with_fallback(path: Path) -> tuple[str, str]:
    """尽力把文件读成文本,返回 (内容, 实际用的编码)。

    最后那一档用 errors="replace" 兜底:宁可显示几个问号,
    也不要因为一个坏字节就把整个审查任务搞崩。
    """
    for encoding in ENCODING_CHAIN:
        try:
            return path.read_text(encoding=encoding), encoding
        except UnicodeDecodeError:
            continue

    return path.read_text(encoding="utf-8", errors="replace"), "utf-8(replace)"


def number_lines(text: str, start_line: int = 1) -> str:
    """给每一行加上行号前缀。"""
    rows = text.splitlines()

    if not rows:
        return ""

    last = start_line + len(rows) - 1
    width = len(str(last))

    numbered = []
    for offset, row in enumerate(rows):
        number = start_line + offset
        numbered.append(LINE_FORMAT.format(number=number, width=width, text=row))

    return "\n".join(numbered)


# ======================================================================
# read_file


class ReadFileTool(Tool):
    name = "read_file"

    description = (
        "读取指定文件的内容，返回带行号的文本。读取代码前必须先调用本工具，"
        "不要凭猜测评价未读过的文件。对于超过上限的大文件，可用 start_line / end_line "
        "分段读取。"
    )

    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件路径，相对于项目根目录，例如 examples/bad_code.py",
            },
            "start_line": {
                "type": "integer",
                "description": "起始行号（从 1 开始），可选。大文件分段读取时使用。",
            },
            "end_line": {
                "type": "integer",
                "description": "结束行号（含），可选。",
            },
        },
        "required": ["path"],
    }

    def run(
        self,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        target = resolve_safe(path)
        self._reject_non_file(target, path)

        size = target.stat().st_size
        text, encoding = read_text_with_fallback(target)
        rows = text.splitlines()
        total = len(rows)

        begin, finish = self._pick_range(rows, total, start_line, end_line)
        selected = rows[begin - 1 : finish]

        self._reject_oversized(size, begin, len(selected), total, start_line, end_line)

        body = "\n".join(selected)
        clipped = False
        if len(body) > RETURN_LIMIT:
            body = body[:RETURN_LIMIT]
            clipped = True

        return {
            "path": self._display_path(target),
            "total_lines": total,
            "encoding": encoding,
            "size_bytes": size,
            "start_line": begin,
            "content": number_lines(body, start_line=begin),
            "truncated": clipped,
        }

    # ------------------------------------------------------------------

    @staticmethod
    def _reject_non_file(target: Path, shown: str) -> None:
        if not target.exists():
            raise ToolError(
                f"File not found: {shown}",
                "确认一下路径写对没有;不确定的话先用 list_dir 看看目录里有什么",
            )

        if target.is_dir():
            raise ToolError(
                f"{shown} 是个目录,不是文件",
                "用 list_dir 看目录内容,或者直接指定目录下的某个文件",
            )

        suffix = target.suffix.lower()
        if suffix in BINARY_SUFFIXES:
            raise ToolError(
                f"{shown} 看着是二进制文件({suffix}),没法当文本读",
                "这个工具只处理源码一类的文本文件",
            )

    @staticmethod
    def _pick_range(
        rows: list[str],
        total: int,
        start_line: int | None,
        end_line: int | None,
    ) -> tuple[int, int]:
        """算出要返回的行区间 [begin, finish],行号从 1 开始。"""
        if start_line is None and end_line is None:
            return 1, total

        begin = 1
        if start_line is not None:
            begin = max(1, int(start_line))

        if begin > total:
            raise ToolError(
                f"起始行 {begin} 超出了文件总行数 {total}",
                f"这个文件一共只有 {total} 行",
            )

        if end_line is None:
            finish = total
        else:
            finish = int(end_line)

        finish = min(finish, total)
        return begin, finish

    @staticmethod
    def _reject_oversized(
        size: int,
        begin: int,
        selected: int,
        total: int,
        start_line: int | None,
        end_line: int | None,
    ) -> None:
        """整体读取时才做体积检查;分段读是允许的。

        否则大文件就彻底没救了 —— 用户既不能整体读,也没法分段读。
        """
        if start_line is not None or end_line is not None:
            return

        if begin != 1 or selected != total:
            return

        limit = config_module.CONFIG.max_file_bytes
        if size <= limit:
            return

        raise ToolError(
            f"文件太大了({size / 1024:.1f} KB,上限 {limit / 1024:.0f} KB),没有整体读取",
            "用 start_line / end_line 分段读,或者换个小一点的目标",
        )

    @staticmethod
    def _display_path(target: Path) -> str:
        try:
            return str(target.relative_to(config_module.CONFIG.workspace_root))
        except ValueError:
            return str(target)


# ======================================================================
# list_dir


class ListDirTool(Tool):
    name = "list_dir"

    description = (
        "列出目录下的文件与子目录，并标注 Python 文件的行数。用于了解项目结构，"
        "或在不知道确切路径时先探查。"
    )

    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "目录路径，相对项目根目录；省略则列出项目根目录。",
            }
        },
        "required": [],
    }

    # 这些目录列出来也没用,还会挤占上下文
    SKIP = {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        "vendor",
        ".tmp",
        "node_modules",
    }

    def run(self, path: str = ".", **_: Any) -> dict[str, Any]:
        target = resolve_safe(path)

        if not target.exists():
            raise ToolError(f"Directory not found: {path}", "确认一下路径写对没有")

        if not target.is_dir():
            raise ToolError(f"{path} 是个文件,不是目录", "直接对它用 read_file 就行")

        files: list[dict[str, Any]] = []
        folders: list[str] = []

        entries = sorted(target.iterdir(), key=lambda item: item.name.lower())
        for entry in entries:
            if entry.name in self.SKIP:
                continue

            if entry.is_dir():
                folders.append(entry.name)
                continue

            files.append(self._describe_file(entry))

        return {
            "path": self._display_path(target),
            "directories": folders,
            "files": files,
            "file_count": len(files),
        }

    @staticmethod
    def _describe_file(entry: Path) -> dict[str, Any]:
        info: dict[str, Any] = {
            "name": entry.name,
            "size_bytes": entry.stat().st_size,
        }

        # 顺手数一下 Python 文件的行数,模型据此能判断该先看哪个文件
        if entry.suffix == ".py":
            info["lines"] = _count_lines(entry)

        return info

    @staticmethod
    def _display_path(target: Path) -> str:
        try:
            relative = target.relative_to(config_module.CONFIG.workspace_root)
        except ValueError:
            return str(target)

        return str(relative) or "."


def _count_lines(path: Path) -> int | None:
    """数一个文件有多少行。读不动就返回 None,不抛异常。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    return len(text.splitlines())
