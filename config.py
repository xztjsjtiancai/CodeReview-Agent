"""运行期配置。

规矩很简单:业务代码里不许出现硬编码的模型名、API Key、体积上限。
需要什么就来这里拿,拿不到就用默认值 —— 缺 .env 也不能让程序起不来。
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv


# 项目根目录,同时也是工具默认允许访问的边界
ROOT = Path(__file__).resolve().parent

# 先把 .env 读进来。已经存在的环境变量优先级更高,不会被文件覆盖。
load_dotenv(ROOT / ".env")

# Key 里出现这些字样,说明用户还没真填(可能是 .env.example 的模板)
FAKE_KEY_MARKERS = ("粘贴", "填入", "replace", "your-key", "sk-xxx")


# ======================================================================
# 环境变量的读取助手
#
# 这几个函数都不抛异常:读不到、读出来是空的、类型转不过去,
# 一律退回默认值。
# ======================================================================


def read_text(name: str, fallback: str) -> str:
    raw = os.getenv(name)
    if raw is None:
        return fallback
    if not raw.strip():
        return fallback
    return raw.strip()


def read_int(name: str, fallback: int) -> int:
    raw = os.getenv(name)
    if not raw or not raw.strip():
        return fallback

    try:
        return int(raw.strip())
    except ValueError:
        return fallback


def read_bool(name: str, fallback: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return fallback

    text = raw.strip().lower()
    if not text:
        return fallback

    return text in {"1", "true", "yes", "on"}


# ======================================================================
# 配置对象
# ======================================================================


@dataclass(frozen=True)
class Config:
    """一份配置快照。做成 frozen 是为了避免运行中被悄悄改掉。"""

    # 模型接入
    api_key: str
    base_url: str
    model: str
    thinking: str
    request_timeout: float
    max_retries: int

    # Agent 行为
    max_steps: int
    max_context_chars: int

    # 工具限制
    max_file_bytes: int
    workspace_root: Path

    # 界面
    ascii_only: bool

    @property
    def thinking_enabled(self) -> bool:
        return self.thinking.lower() == "enabled"

    @property
    def has_real_key(self) -> bool:
        """Key 是否看起来是真的填过。"""
        return not is_placeholder_key(self.api_key)


def load_config() -> Config:
    """从环境变量拼出一份配置。"""
    return Config(
        api_key=read_text("DEEPSEEK_API_KEY", ""),
        base_url=read_text("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        model=read_text("DEEPSEEK_MODEL", "deepseek-flash"),
        # 一期关掉思考模式。开了之后,带 tools 的请求必须把历史里每一轮的
        # reasoning_content 都回传,漏一条服务端就返回 400。
        thinking=read_text("CRA_THINKING", "disabled"),
        request_timeout=float(read_int("CRA_REQUEST_TIMEOUT", 60)),
        max_retries=read_int("CRA_MAX_RETRIES", 3),
        max_steps=read_int("CRA_MAX_STEPS", 8),
        max_context_chars=read_int("CRA_MAX_CONTEXT_CHARS", 120000),
        max_file_bytes=read_int("CRA_MAX_FILE_BYTES", 204800),
        workspace_root=Path(read_text("CRA_WORKSPACE_ROOT", str(ROOT))).resolve(),
        ascii_only=read_bool("CRA_ASCII", False),
    )


# ======================================================================
# Key 的显示与判断
# ======================================================================


def mask_secret(value: str) -> str:
    """打码后再展示,免得截图或日志把 Key 泄出去。"""
    if not value:
        return "(未设置)"

    if len(value) <= 8:
        return "*" * len(value)

    head = value[:6]
    tail = value[-4:]
    return f"{head}...{tail}"


def is_placeholder_key(key: str) -> bool:
    """判断 Key 是不是空的或者还是模板里的占位文字。

    光判断非空是不够的:.env 里那句
    "<在这里粘贴你的-DeepSeek-API-Key>" 长度完全达标,
    但显然不能用,所以还要看有没有占位标记。
    """
    if not key:
        return True

    text = key.strip()
    if len(text) < 8:
        return True

    if text.startswith("<"):
        return True

    lowered = text.lower()
    for marker in FAKE_KEY_MARKERS:
        if marker in lowered:
            return True

    return False


# 模块级默认配置。`import config` 之后直接用 config.CONFIG 即可。
CONFIG: Config = load_config()
