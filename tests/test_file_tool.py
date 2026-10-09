"""文件工具边界测试。

覆盖《项目构建指导文档》§14.1 中与文件相关的全部边界情况：
不存在、是目录、空文件、超大文件、二进制、GBK 编码、非 UTF-8、
行范围读取越界、路径逃逸（安全）、以及单次返回的截断。
全部无需 API Key，可离线运行。

关于测试目录：这里刻意不使用 pytest 的 ``tmp_path``。受限环境下系统临时目录
不可写，且 pytest 会在会话结束时遍历其基准临时目录，从而触发权限错误。
改为在 ``tests/_workdir/`` 下造样本，并把工具的工作目录切到这里，
既隔离又便于清理。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
import shutil
import sys
import types

import pytest

from agent.errors import ToolError
from tools.base import Tool
from tools.file_tool import ListDirTool
from tools.file_tool import ReadFileTool
from tools.file_tool import number_lines
from tools.file_tool import read_text_with_fallback
from tools.registry import ToolRegistry


WORKDIR = Path(__file__).resolve().parent / "_workdir"


@pytest.fixture()
def sandbox(monkeypatch: pytest.MonkeyPatch) -> Path:
    """提供一个干净的工作目录，并把工具看到的 workspace_root 指向它。"""
    import config as config_module  # noqa: F401  确保配置模块已加载
    import tools.file_tool  # noqa: F401  确保工具模块在替换前已加载

    shutil.rmtree(WORKDIR, ignore_errors=True)
    WORKDIR.mkdir(parents=True, exist_ok=True)

    original = sys.modules["config"]
    patched = types.ModuleType("config")
    patched.__dict__.update(vars(original))
    patched.CONFIG = dataclasses.replace(original.CONFIG, workspace_root=WORKDIR)

    monkeypatch.setitem(sys.modules, "config", patched)
    monkeypatch.setattr(sys.modules["tools.file_tool"], "config_module", patched, raising=False)
    yield WORKDIR
    shutil.rmtree(WORKDIR, ignore_errors=True)


def _set_max_file_bytes(monkeypatch: pytest.MonkeyPatch, value: int) -> None:
    """在当前（可能已被 sandbox 夹具替换的）配置上调整文件大小上限。"""
    current = sys.modules["config"]
    current.CONFIG = dataclasses.replace(current.CONFIG, max_file_bytes=value)
    monkeypatch.setattr(sys.modules["tools.file_tool"], "config_module", current, raising=False)


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------


def test_reads_file_with_line_numbers(sandbox: Path):
    (sandbox / "a.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    result = ReadFileTool().run(path="a.py")
    assert result["total_lines"] == 2
    assert result["encoding"] == "utf-8"
    assert "1 | x = 1" in result["content"]
    assert "2 | y = 2" in result["content"]
    assert result["truncated"] is False


def test_line_range_reading(sandbox: Path):
    (sandbox / "many.py").write_text("\n".join(f"line{i}" for i in range(1, 21)), encoding="utf-8")
    result = ReadFileTool().run(path="many.py", start_line=5, end_line=7)
    assert result["start_line"] == 5
    assert "5 | line5" in result["content"]
    assert "7 | line7" in result["content"]
    assert "line8" not in result["content"]
    assert result["total_lines"] == 20


def test_start_line_beyond_end_is_rejected(sandbox: Path):
    (sandbox / "tiny.py").write_text("a = 1\n", encoding="utf-8")
    with pytest.raises(ToolError) as exc:
        ReadFileTool().run(path="tiny.py", start_line=99)
    assert "超出" in str(exc.value) and "总行数" in str(exc.value)


# ---------------------------------------------------------------------------
# 错误路径：全部应转成 ToolError（进而被 Agent 当作 Observation 回灌）
# ---------------------------------------------------------------------------


def test_missing_file_raises_tool_error(sandbox: Path):
    with pytest.raises(ToolError) as exc:
        ReadFileTool().run(path="nope.py")
    assert "File not found" in exc.value.message
    assert "list_dir" in exc.value.hint


def test_directory_is_rejected(sandbox: Path):
    (sandbox / "pkg").mkdir()
    with pytest.raises(ToolError) as exc:
        ReadFileTool().run(path="pkg")
    assert "目录" in exc.value.message


def test_binary_file_is_rejected(sandbox: Path):
    (sandbox / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")
    with pytest.raises(ToolError) as exc:
        ReadFileTool().run(path="img.png")
    assert "二进制" in exc.value.message


def test_empty_file_is_read_without_error(sandbox: Path):
    (sandbox / "empty.py").write_text("", encoding="utf-8")
    result = ReadFileTool().run(path="empty.py")
    assert result["total_lines"] == 0
    assert result["content"] == ""


# ---------------------------------------------------------------------------
# 编码兼容：中文 Windows 的现实情况
# ---------------------------------------------------------------------------


def test_gbk_file_is_read_with_fallback_encoding(sandbox: Path):
    (sandbox / "gbk.py").write_bytes("# 中文注释\nx = 1\n".encode("gbk"))
    result = ReadFileTool().run(path="gbk.py")
    assert result["encoding"] == "gbk"
    assert "中文注释" in result["content"]


def test_undecodable_bytes_fall_back_to_replacement(sandbox: Path):
    (sandbox / "weird.py").write_bytes(b"\xff\xfe\x00\x01invalid\nx = 1\n")
    result = ReadFileTool().run(path="weird.py")
    assert result["encoding"] == "utf-8(replace)"
    assert result["total_lines"] >= 1


def test_read_text_with_fallback_returns_encoding(sandbox: Path):
    path = sandbox / "u.py"
    path.write_text("你好\n", encoding="utf-8")
    text, encoding = read_text_with_fallback(path)
    assert encoding == "utf-8"
    assert "你好" in text


# ---------------------------------------------------------------------------
# 体积上限与截断
# ---------------------------------------------------------------------------


def test_oversized_file_is_rejected_whole_read(sandbox: Path, monkeypatch: pytest.MonkeyPatch):
    _set_max_file_bytes(monkeypatch, 100)
    (sandbox / "big.py").write_text("x = 1\n" * 200, encoding="utf-8")
    with pytest.raises(ToolError) as exc:
        ReadFileTool().run(path="big.py")
    assert "文件太大" in exc.value.message


def test_oversized_file_can_still_be_read_in_ranges(sandbox: Path, monkeypatch: pytest.MonkeyPatch):
    _set_max_file_bytes(monkeypatch, 100)
    (sandbox / "big.py").write_text("x = 1\n" * 200, encoding="utf-8")
    result = ReadFileTool().run(path="big.py", start_line=1, end_line=3)
    assert result["total_lines"] == 200
    assert "1 | x = 1" in result["content"]


# ---------------------------------------------------------------------------
# 安全：路径逃逸
# ---------------------------------------------------------------------------


def test_path_escape_is_rejected(sandbox: Path):
    with pytest.raises(ToolError) as exc:
        ReadFileTool().run(path="../../secret.txt")
    assert "工作目录之外" in exc.value.message


def test_absolute_path_outside_workspace_is_rejected(sandbox: Path):
    with pytest.raises(ToolError):
        ReadFileTool().run(path="C:/Windows/system.ini")


def test_subdirectory_inside_workspace_is_allowed(sandbox: Path):
    (sandbox / "sub").mkdir()
    (sandbox / "sub" / "inner.py").write_text("ok = 1\n", encoding="utf-8")
    result = ReadFileTool().run(path="sub/inner.py")
    assert "inner.py" in result["path"]


# ---------------------------------------------------------------------------
# list_dir
# ---------------------------------------------------------------------------


def test_list_dir_reports_files_and_skips_noise(sandbox: Path):
    (sandbox / "a.py").write_text("x = 1\n", encoding="utf-8")
    (sandbox / "readme.txt").write_text("hi", encoding="utf-8")
    (sandbox / "__pycache__").mkdir()
    (sandbox / "__pycache__" / "junk.pyc").write_bytes(b"\x00")

    result = ListDirTool().run(path=".")
    names = {item["name"] for item in result["files"]}
    assert names == {"a.py", "readme.txt"}
    assert "__pycache__" not in result["directories"]
    assert next(i for i in result["files"] if i["name"] == "a.py")["lines"] == 1


def test_list_dir_on_file_is_rejected(sandbox: Path):
    (sandbox / "a.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(ToolError):
        ListDirTool().run(path="a.py")


# ---------------------------------------------------------------------------
# number_lines 辅助函数
# ---------------------------------------------------------------------------


def test_number_lines_aligns_width_for_large_line_counts():
    text = "\n".join(f"l{i}" for i in range(1, 12))
    rendered = number_lines(text, start_line=1)
    lines = rendered.splitlines()
    assert lines[0].startswith(" 1 | l1")
    assert lines[9].startswith("10 | l10")
    assert lines[10].startswith("11 | l11")


# ---------------------------------------------------------------------------
# Registry 集成：工具通过注册表派发时行为一致
# ---------------------------------------------------------------------------


def test_registry_dispatch_returns_structured_result(sandbox: Path):
    (sandbox / "a.py").write_text("x = 1\n", encoding="utf-8")
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    result = registry.dispatch("read_file", {"path": "a.py"})
    assert result["total_lines"] == 1


def test_registry_wraps_tool_error_without_leaking_exception(sandbox: Path):
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    with pytest.raises(ToolError):
        registry.dispatch("read_file", {"path": "missing.py"})


def test_tool_is_abstract_and_cannot_be_instantiated():
    with pytest.raises(TypeError):
        Tool()  # type: ignore[abstract]
