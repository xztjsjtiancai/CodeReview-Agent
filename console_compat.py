"""终端编码兼容。

中文 Windows 的控制台默认代码页是 GBK。直接打印 emoji 或者框线字符
(比如 ╔)会抛 UnicodeEncodeError,程序当场崩掉 —— 这是演示时最容易翻车的地方。
所以在任何输出发生之前,先把这个补丁打上。
"""

from __future__ import annotations

import sys
from typing import Any

from rich.console import Console


def setup_console() -> None:
    """把标准输出和标准错误切到 UTF-8。启动时调用一次即可。

    对已经是 UTF-8 的流不做任何事(避免重复设置)。某些环境下流对象
    没有 reconfigure 方法,这种情况直接跳过,让程序继续跑。
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)

        if stream is None:
            continue

        current = getattr(stream, "encoding", "")
        if current and "utf" in current.lower():
            continue

        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue

        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 兜底,设置失败也不能让程序起不来
            pass


def make_console() -> Console:
    """建一个 rich 控制台。

    关掉 legacy_windows 是为了让 rich 走 ANSI 转义序列;Windows 10 以后
    的终端都支持,颜色和框线才正常。万一环境不支持,rich 自己会退化成
    纯文本,不会抛异常。
    """
    return Console(highlight=False, soft_wrap=False, legacy_windows=False)


def safe_print(*values: Any, **kwargs: Any) -> None:
    """不会因为编码问题炸掉的 print。

    正常情况下就是普通 print;只有当目标流编不出某个字符时,
    才退一步把不可编码的字符替换掉再打。
    """
    text = " ".join(str(item) for item in values)

    try:
        print(text, **kwargs)
        return
    except UnicodeEncodeError:
        pass

    stream = kwargs.get("file") or sys.stdout
    encoding = getattr(stream, "encoding", None) or "ascii"
    fallback = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    print(fallback, **kwargs)
