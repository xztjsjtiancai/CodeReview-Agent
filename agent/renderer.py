"""把 Agent 事件画到终端上。

这一层只做「事件 → 文字」的翻译,不做任何业务判断。

为什么要留一个 ASCII 模式:CMD 默认代码页是 GBK,emoji 和框线字符
在里面可能显示成乱码,严重的还会直接抛 UnicodeEncodeError。
演示的时候翻车代价太大,所以给一个开关能全部退化成纯 ASCII。
"""

from __future__ import annotations

import json
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from config import Config
from config import mask_secret


# 面板固定宽度。不固定的话,输出被重定向到文件时 rich 会按 80 列排版,
# 中文长句会被硬折,看起来很难受。
PANEL_WIDTH = 100

# 界面上的品牌标识
APP_NAME = "xztcode"
APP_TAGLINE = "AI Code Review Agent"
APP_VERSION = "v1.0"

# 用户输入提示符。做成 "xztcode > " 的样子,和命令行工具的习惯一致。
PROMPT = "xztcode > "

# 严重等级对应的图标和颜色
LEVEL_ICON = {
    "high": ("🔴", "bold red"),
    "medium": ("🟡", "yellow"),
    "low": ("🟢", "green"),
}

# ASCII 模式下改用文字标签顶替图标
LEVEL_TEXT = {
    "high": "[HIGH]  ",
    "medium": "[MEDIUM]",
    "low": "[LOW]   ",
}

# 参数行和兜底摘要的截断长度
ARG_LIMIT = 160
FALLBACK_LIMIT = 120

# 方框标题的宽度(不含两侧边框)
BANNER_INNER = 46


class Renderer:
    """所有面向用户的输出都从这里走。"""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.ascii_only = config.ascii_only
        self.console = Console(
            highlight=False,
            soft_wrap=False,
            width=PANEL_WIDTH,
            legacy_windows=False,
        )

    # ==================================================================
    # 开场

    def banner(self) -> None:
        self.console.print(f"[bold cyan]{self._banner_text()}[/]")

        line = (
            f"[dim]Model: {self.config.model}  |  "
            f"Thinking: {self.config.thinking}  |  "
            f"Max steps: {self.config.max_steps}  |  "
            f"Key: {mask_secret(self.config.api_key)}[/]"
        )
        self.console.print(line)
        self.console.print(f"[dim]工作目录: {self.config.workspace_root}[/]")
        self.console.print("[dim]输入 /help 查看命令，/exit 退出。[/]\n")

    def _banner_text(self) -> str:
        """做那个方框标题。ASCII 模式下换成 '+' 和 '-'。"""
        title = f"{APP_NAME} {APP_VERSION}"
        inner = BANNER_INNER

        if self.ascii_only:
            edge = "+" + "-" * inner + "+"
            rows = [
                "|" + title.center(inner) + "|",
                "|" + APP_TAGLINE.center(inner) + "|",
            ]
            return "\n".join([edge, *rows, edge])

        top = "╔" + "═" * inner + "╗"
        rows = [
            "║" + title.center(inner) + "║",
            "║" + APP_TAGLINE.center(inner) + "║",
        ]
        bottom = "╚" + "═" * inner + "╝"
        return "\n".join([top, *rows, bottom])

    # ==================================================================
    # 事件分发

    def render_event(self, event: dict[str, Any]) -> None:
        kind = event.get("type")

        if kind == "assistant":
            self._show_assistant(str(event.get("content", "")))
            return

        if kind == "tool_start":
            self._show_tool_start(event)
            return

        if kind == "tool_end":
            self._show_tool_end(event)
            return

        if kind == "error":
            self.console.print(f"[bold red]! {event.get('message', '')}[/]")
            return

        # 其他类型(比如 final)由 main 负责渲染,这里不处理

    def _show_assistant(self, content: str) -> None:
        if not content.strip():
            return
        self.console.print()
        self.console.print(f"[bold green]xztcode[/] {content}")

    def _show_tool_start(self, event: dict[str, Any]) -> None:
        name = event.get("name", "")
        args = self._format_args(event.get("arguments", {}))
        self.console.print(f"  [cyan]→ {name}[/] [dim]{args}[/]")

    def _show_tool_end(self, event: dict[str, Any]) -> None:
        name = str(event.get("name", ""))
        result = event.get("result") or {}

        if event.get("ok"):
            self.console.print(f"     [green]✓[/] {self._summarize(name, result)}")
            return

        reason = result.get("error", "未知错误")
        self.console.print(f"     [red]✗[/] {reason}")

    # ==================================================================
    # 结果摘要

    def _summarize(self, name: str, result: dict[str, Any]) -> str:
        """把工具结果压成一行,让用户跟得上 Agent 的动作。"""
        if result.get("cached"):
            return "[dim]（命中缓存，未重复执行）[/]"

        if "error" in result:
            return f"[red]{result['error']}[/]"

        if name == "read_file":
            return self._summarize_read(result)

        if name == "ast_analyzer":
            return self._summarize_ast(result)

        if name == "search_code":
            return f"匹配 {result.get('match_count', 0)} 处"

        if name == "list_dir":
            return f"{result.get('file_count', 0)} 个文件"

        # 以后加了新工具,先给个原始预览,不至于什么都不显示
        return json.dumps(result, ensure_ascii=False)[:FALLBACK_LIMIT]

    @staticmethod
    def _summarize_read(result: dict[str, Any]) -> str:
        total = result.get("total_lines", "?")
        path = result.get("path", "")

        if result.get("truncated"):
            return f"{path} · {total} 行（已截断）"

        return f"{path} · {total} 行"

    @staticmethod
    def _summarize_ast(result: dict[str, Any]) -> str:
        if not result.get("syntax_ok", True):
            return "发现语法错误，无法完成静态分析"

        counts = result.get("summary", {})
        high = counts.get("high", 0)
        medium = counts.get("medium", 0)
        low = counts.get("low", 0)
        total = result.get("issue_count", 0)

        return f"{total} 个问题 (high {high} / medium {medium} / low {low})"

    @staticmethod
    def _format_args(arguments: Any) -> str:
        if not isinstance(arguments, dict):
            return str(arguments)[:FALLBACK_LIMIT]

        if not arguments:
            return ""

        parts = []
        for key, value in arguments.items():
            parts.append(f"{key}={value}")

        return " ".join(parts)[:ARG_LIMIT]

    # ==================================================================
    # 最终报告

    def render_final(self, content: str) -> None:
        body = content.strip() or "(空)"
        panel = Panel(Text(body), title=f"{APP_NAME.upper()} · REVIEW REPORT", border_style="cyan")

        self.console.print()
        self.console.print(panel)
        self.console.print()

    # ==================================================================
    # 命令的输出

    def help(self) -> None:
        rows = [
            ("/help", "显示本帮助"),
            ("/review <路径>", "直接审查指定文件"),
            ("/tools", "列出已注册的工具及其参数"),
            ("/history", "查看本次会话的对话与工具调用记录"),
            ("/clear", "清空上下文（重新开始一段对话）"),
            ("/exit 或 /quit", "退出程序"),
            (None, None),
            ("其他任意输入", "当作自然语言交给 Agent 处理"),
        ]

        self.console.print("[bold]可用命令[/]")

        for command, description in rows:
            if command is None:
                self.console.print()
                continue
            self.console.print(f"  [cyan]{command:<18}[/] {description}")

        self.console.print()

    def tools(self, descriptions: list[dict[str, Any]]) -> None:
        self.console.print(f"[bold]已注册工具（{len(descriptions)} 个）[/]")

        for item in descriptions:
            self._show_one_tool(item)

        self.console.print()

    def _show_one_tool(self, item: dict[str, Any]) -> None:
        self.console.print(f"\n  [bold cyan]{item['name']}[/]")
        self.console.print(f"    [dim]{item['description']}[/]")

        spec = item.get("parameters", {})
        properties = spec.get("properties", {})
        required = set(spec.get("required", []))

        for name, detail in properties.items():
            if name in required:
                flag = "必填"
            else:
                flag = "可选"

            kind = detail.get("type", "?")
            note = detail.get("description", "")
            self.console.print(f"      - {name} ({kind}, {flag}) [dim]{note}[/]")

    def history(self, trail: list[dict[str, Any]]) -> None:
        if not trail:
            self.console.print("[dim]本次会话还没有记录。[/]\n")
            return

        self.console.print("[bold]会话记录[/]")

        for entry in trail:
            self._show_one_trail_entry(entry)

        self.console.print()

    def _show_one_trail_entry(self, entry: dict[str, Any]) -> None:
        kind = entry.get("kind")

        if kind == "user":
            self.console.print(f"\n[bold]You[/] {entry.get('content', '')}")
            return

        if kind == "assistant":
            self.console.print(f"[bold green]xztcode[/] {entry.get('content', '')}")
            return

        if kind == "tool":
            if entry.get("ok"):
                mark = "[green]✓[/]"
            else:
                mark = "[red]✗[/]"
            self.console.print(f"  {mark} [cyan]{entry.get('name', '')}[/]")

    # ==================================================================
    # 短消息

    def info(self, message: str) -> None:
        self.console.print(f"[dim]{message}[/]")

    def warn(self, message: str) -> None:
        self.console.print(f"[yellow]! {message}[/]")

    def error(self, message: str, hint: str = "") -> None:
        self.console.print(f"[bold red]错误：{message}[/]")
        if hint:
            self.console.print(f"  [yellow]↳ {hint}[/]")

    # ==================================================================
    # 等级标签(给命令层预留)

    def severity_label(self, severity: str) -> tuple[str, str]:
        if self.ascii_only:
            return LEVEL_TEXT.get(severity, "[?]      "), "white"
        return LEVEL_ICON.get(severity, ("⚪", "white"))
