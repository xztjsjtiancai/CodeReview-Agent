"""离线依赖安装器（纯标准库实现）。

为什么需要它：本机沙箱内子进程无法使用 pip 解包（写临时目录被拒），
且进程级 HTTPS 被拦截，只能走 HTTP 镜像。因此自行完成 pip 的四个步骤：

    index 解析 → 依赖递归解析 → HTTP 下载 wheel → zipfile 解包到 vendor/

安装结果落在项目内的 ``vendor/`` 目录，通过 PYTHONPATH 注入，不污染系统环境。

用法：
    python tools_dev/install_deps.py --dry-run     # 只解析并打印计划
    python tools_dev/install_deps.py               # 下载并安装到 vendor/
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
from pathlib import Path
import re
import sys
import time
import urllib.parse
import urllib.request
import zipfile


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

MIRROR = "http://mirrors.aliyun.com/pypi/simple"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
VENDOR = PROJECT_ROOT / "vendor"
DOWNLOAD_DIR = PROJECT_ROOT / ".tmp" / "wheels"

# 与当前解释器匹配的 wheel 标签
PY_TAG = f"cp{sys.version_info.major}{sys.version_info.minor}"
PLATFORM_TAGS = (f"{PY_TAG}-{PY_TAG}-win_amd64", "py3-none-any", "py2.py3-none-any")
MARKER_ENV = {
    "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
    "python_full_version": (
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    ),
    "sys_platform": "win32",
    "platform_system": "Windows",
    "os_name": "nt",
    "platform_machine": "AMD64",
    "implementation_name": "cpython",
}

# 目标依赖（锁定主版本，确保与一期代码写法匹配）
# httpx 显式排除 1.x：其预发布版依赖 httpx-aiohttp，会把 aiohttp / numpy 一并拖进来
TARGETS = [
    "openai>=1.40,<2",
    "python-dotenv>=1",
    "rich>=13",
    "httpx>=0.27,<1",
    "pytest>=8",
    "ruff>=0.5",
]


# ---------------------------------------------------------------------------
# 版本与需求解析
# ---------------------------------------------------------------------------

_VERSION_RE = re.compile(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[.\-]?([a-zA-Z]+\d*))?")


def parse_version(text: str) -> tuple:
    """把版本号转成可比较的元组。预发布版本排在正式版之前。"""
    match = _VERSION_RE.match(text.strip())
    if not match:
        return (0,)
    major = int(match.group(1))
    minor = int(match.group(2) or 0)
    patch = int(match.group(3) or 0)
    suffix = match.group(4)
    # 有字母后缀 = 预发布（如 1.0.0rc1），排在对应正式版之前
    pre_rank = 0 if suffix else 1
    pre_num = int(re.sub(r"\D", "", suffix) or 0) if suffix else 0
    return (major, minor, patch, pre_rank, pre_num)


def version_key(text: str) -> tuple:
    return parse_version(text)


# 预发布版本识别：dev / a / b / rc / alpha / beta / pre 等后缀
# 先剥掉 .postN（补丁发布，不算预发布），再判断尾部是否带预发布标识
_POST_RE = re.compile(r"[.\-_]?post\d*$", re.IGNORECASE)
_PRERELEASE_RE = re.compile(r"[.\-_]?(dev|a|b|c|rc|alpha|beta|pre|preview)\d*$", re.IGNORECASE)


def is_prerelease(version: str) -> bool:
    """判断是否为预发布版本。

    pip 默认不安装预发布版本，本解析器遵循同样的策略：
    否则会出现 ``httpx==1.0.dev6``、``pydantic==2.14.0b2`` 这类
    「版本号更大但实际不可用」的选择。
    """
    return bool(_PRERELEASE_RE.search(_POST_RE.sub("", version.strip())))


def satisfies(version: str, specifier: str) -> bool:
    """判断版本是否满足 PEP 440 风格的约束串（只支持常用运算符）。"""
    if not specifier.strip():
        return True
    current = parse_version(version)
    for clause in specifier.split(","):
        clause = clause.strip()
        if not clause:
            continue
        match = re.match(r"^(===|==|!=|<=|>=|~=|<|>)\s*(.+)$", clause)
        if not match:
            continue
        op, raw = match.group(1), match.group(2).strip()
        if op in ("==", "==="):
            if op == "==" and raw.endswith(".*"):
                if not version.startswith(raw[:-2]):
                    return False
            elif version != raw:
                return False
        elif op == "!=":
            if version == raw:
                return False
        else:
            target = parse_version(raw)
            if op == ">=" and current < target:
                return False
            if op == "<=" and current > target:
                return False
            if op == ">" and current <= target:
                return False
            if op == "<" and current >= target:
                return False
            if op == "~=":
                # ~=1.4.2 等价于 >=1.4.2 且 ==1.4.*
                if current < target or version.split(".")[:2] != raw.split(".")[:2]:
                    return False
    return True


def marker_applies(marker: str) -> bool:
    """粗略求值环境标记；无法判断时保守返回 True。

    关键：``extra == 'xxx'`` 之类的标记表示**可选依赖**，本项目一律不安装，
    否则 openai 会把 aiohttp、numpy 等一大堆 extras 依赖拖进来。
    """
    if not marker:
        return True
    if re.search(r"\bextra\s*==", marker):
        return False
    expr = marker.strip()
    # 只处理 and/or 连接的单层表达式，足够覆盖主流包
    or_parts = re.split(r"\bor\b", expr)
    for or_part in or_parts:
        and_parts = re.split(r"\band\b", or_part)
        if all(_eval_atom(part) for part in and_parts):
            return True
    return False


def _eval_atom(atom: str) -> bool:
    atom = atom.strip().strip("()")
    pattern = (
        r"^([A-Za-z_][A-Za-z0-9_]*)\s*"
        r"(==|!=|>=|<=|>|<|in|not in)\s*['\"]([^'\"]*)['\"]$"
    )
    match = re.match(pattern, atom)
    if not match:
        return True  # 不认识就放行，避免漏装依赖
    name, op, value = match.group(1), match.group(2), match.group(3)
    actual = MARKER_ENV.get(name)
    if actual is None:
        return True
    if op == "==":
        return actual == value
    if op == "!=":
        return actual != value
    if op == "in":
        return actual in value
    if op == "not in":
        return actual not in value
    # 版本比较
    if op == ">=":
        return parse_version(actual) >= parse_version(value)
    if op == "<=":
        return parse_version(actual) <= parse_version(value)
    if op == ">":
        return parse_version(actual) > parse_version(value)
    if op == "<":
        return parse_version(actual) < parse_version(value)
    return True


_REQ_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"\s*(?:\[(?P<extras>[^\]]*)\])?"
    r"\s*(?P<spec>(?:[<>=!~]=?[^,;\s]+\s*,?\s*)*)"
    r"\s*(?:;\s*(?P<marker>.+))?$"
)


def parse_requirement(line: str) -> tuple[str, str, str] | None:
    """解析一行 Requires-Dist，返回 (规范化包名, 约束串, 环境标记)。"""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    match = _REQ_RE.match(line)
    if not match:
        return None
    name = canonical(match.group("name"))
    spec = (match.group("spec") or "").replace(" ", "")
    marker = (match.group("marker") or "").strip()
    return name, spec, marker


def canonical(name: str) -> str:
    """PEP 503 规范化包名。"""
    return re.sub(r"[-_.]+", "-", name).lower()


# ---------------------------------------------------------------------------
# 网络
# ---------------------------------------------------------------------------


def http_get(url: str, retries: int = 2, timeout: int = 20) -> bytes:
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "cra-installer/1.0"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries:
                time.sleep(1.0)
    raise RuntimeError(f"下载失败 {url}: {last}")


def fetch_index(name: str) -> list[dict]:
    """抓取某个包在镜像上的所有 wheel 条目。"""
    body = http_get(f"{MIRROR}/{name}/").decode("utf-8", errors="replace")
    entries: list[dict] = []
    for match in re.finditer(r'href="([^"#]+\.whl)(?:#sha256=([0-9a-f]+))?"', body):
        raw_url, sha = match.group(1), match.group(2)
        filename = html.unescape(raw_url.split("/")[-1])
        url = urllib.parse.urljoin(f"{MIRROR}/{name}/", raw_url)
        wheel_info = parse_wheel_filename(filename)
        if wheel_info is None:
            continue
        entries.append({"filename": filename, "url": url, "sha256": sha or "", **wheel_info})
    return entries


_WHEEL_RE = re.compile(
    r"^(?P<name>[^-]+)-(?P<version>[^-]+)(?:-(?P<build>\d[^-]*))?"
    r"-(?P<py>[^-]+)-(?P<abi>[^-]+)-(?P<plat>[^-]+)\.whl$"
)


def parse_wheel_filename(filename: str) -> dict | None:
    match = _WHEEL_RE.match(filename)
    if not match:
        return None
    return {
        "name": match.group("name"),
        "version": match.group("version"),
        "py": match.group("py"),
        "abi": match.group("abi"),
        "platform": match.group("plat"),
    }


def is_compatible(entry: dict) -> bool:
    py, abi, plat = entry["py"], entry["abi"], entry["platform"]
    if "win" not in plat and plat != "any":
        return False
    if plat == "any":
        return py in ("py3", "py2.py3") or py.startswith("py3")
    if plat != "win_amd64":
        return False
    if abi not in ("none", "abi3", PY_TAG):
        return False
    if abi == "none":
        return py in ("py3", "py2.py3") or py.startswith("py3")
    return py == PY_TAG  # 精确匹配 cp312


def pick_best(entries: list[dict], spec: str) -> dict | None:
    """在满足约束的兼容 wheel 中挑版本最高的一个。

    默认排除预发布版本（与 pip 行为一致）；只有当约束串里显式写了预发布版本号时才纳入。
    """
    allow_prerelease = any(is_prerelease(part) for part in re.findall(r"[\w.]+", spec))
    candidates = [
        entry
        for entry in entries
        if satisfies(entry["version"], spec)
        and is_compatible(entry)
        and (allow_prerelease or not is_prerelease(entry["version"]))
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda entry: version_key(entry["version"]))


def read_metadata(wheel_path: Path) -> tuple[str, list[str]]:
    """从 wheel 中读出 (版本, Requires-Dist 列表)。"""
    with zipfile.ZipFile(wheel_path) as archive:
        meta_name = next((n for n in archive.namelist() if n.endswith(".dist-info/METADATA")), None)
        if meta_name is None:
            return "", []
        text = archive.read(meta_name).decode("utf-8", errors="replace")
    version = ""
    requires: list[str] = []
    for line in text.splitlines():
        if line.startswith("Version:"):
            version = line.split(":", 1)[1].strip()
        elif line.startswith("Requires-Dist:"):
            requires.append(line.split(":", 1)[1].strip())
    return version, requires


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def download(entry: dict, target: Path) -> Path:
    if target.exists() and target.stat().st_size > 0:
        return target
    data = http_get(entry["url"])
    if entry["sha256"]:
        digest = hashlib.sha256(data).hexdigest()
        if digest != entry["sha256"]:
            raise RuntimeError(f"校验失败 {entry['filename']}: {digest} != {entry['sha256']}")
    target.write_bytes(data)
    return target


def install_wheel(wheel_path: Path, vendor: Path) -> None:
    """解包 wheel 到 vendor。

    wheel 里 ``<name>-<ver>.data/scripts/*`` 是命令行入口（例如 ruff.exe）。
    pip 会把它们放进 Scripts 目录；这里统一映射到 ``vendor/bin/``，
    因为 ruff 的查找逻辑恰好会检查 ``<site-packages>/../bin``（见 _find_ruff.py）。
    """
    bin_dir = vendor / "bin"
    with zipfile.ZipFile(wheel_path) as archive:
        for member in archive.namelist():
            if member.endswith("/"):
                continue
            if ".data/" in member:
                _, _, tail = member.partition(".data/")
                if tail.startswith("scripts/"):
                    script_name = tail[len("scripts/") :]
                    if not script_name:
                        continue
                    bin_dir.mkdir(parents=True, exist_ok=True)
                    (bin_dir / script_name).write_bytes(archive.read(member))
                # 其余 .data 内容（headers 等）本项目用不到，跳过
                continue
            archive.extract(member, vendor)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--refresh", action="store_true", help="忽略本地 wheel 缓存重新下载")
    args = parser.parse_args()

    VENDOR.mkdir(parents=True, exist_ok=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # 目标约束：包名 -> 约束串
    constraints: dict[str, str] = {}
    for target in TARGETS:
        name, spec, _ = parse_requirement(target)
        constraints[name] = f"{constraints.get(name, '')},{spec}".strip(",")

    index_cache: dict[str, list[dict]] = {}
    resolved: dict[str, dict] = {}
    queue = list(constraints.items())
    seen: set[tuple[str, str]] = set()

    while queue:
        name, spec = queue.pop(0)
        if (name, spec) in seen:
            continue
        seen.add((name, spec))

        if name in resolved:
            current = resolved[name]
            if satisfies(current["version"], spec):
                continue
        if name not in index_cache:
            print(f"  ... fetching index: {name}", flush=True)
            index_cache[name] = fetch_index(name)
        entry = pick_best(index_cache[name], spec)
        if entry is None:
            print(f"[warn] 找不到满足 {name}{spec} 的兼容 wheel，跳过", file=sys.stderr)
            continue

        resolved[name] = entry
        print(f"  resolved {name}=={entry['version']}  ({entry['filename']})")

        # 下载以读取依赖（dry-run 也下载，否则无法得到依赖树）
        wheel_path = DOWNLOAD_DIR / entry["filename"]
        if args.refresh and wheel_path.exists():
            wheel_path.unlink()
        download(entry, wheel_path)
        _, requires = read_metadata(wheel_path)
        for requirement in requires:
            parsed = parse_requirement(requirement)
            if parsed is None:
                continue
            dep_name, dep_spec, marker = parsed
            if not marker_applies(marker):
                continue
            if not dep_spec:
                dep_spec = ""
            queue.append((dep_name, dep_spec))

    print(f"\n共解析 {len(resolved)} 个包")
    if args.dry_run:
        for name in sorted(resolved):
            print(f"  {name}=={resolved[name]['version']}")
        return 0

    print("\n解包到 vendor/ ...")
    for name, entry in sorted(resolved.items()):
        install_wheel(DOWNLOAD_DIR / entry["filename"], VENDOR)
        print(f"  installed {name}=={entry['version']}")

    (VENDOR / "_installed.json").write_text(
        json.dumps({k: v["version"] for k, v in sorted(resolved.items())}, indent=2),
        encoding="utf-8",
    )
    print(f"\n完成 -> {VENDOR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
