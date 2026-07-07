"""内置工具集合 —— 阶段一扩展版（含 P1 修复）

新增工具：
- get_weather: 天气查询（wttr.in 免费 API）
- search_web:  网络搜索（DuckDuckGo HTML + IA 双端点，浏览器 UA + 重试）
- get_news:    新闻获取（多源 RSS，可配置，失败自动回退）
- read_url:    网页内容读取（含 SSRF/内网防护）

P1 修复记录：
1. search_web 原直接调用 api.duckduckgo.com，覆盖窄且易 403。
   现改为浏览器 UA + 指数退避重试 + HTML 端点优先、IA 端点兜底。
2. get_news 原写死 36kr 单一源。现支持 科技/数码/综合 多源及自定义
   RSS URL，失败时回退 36kr。
3. read_url 原仅校验 scheme，存在 SSRF 风险（可访问内网/云元数据）。
   现新增 _validate_url_ssrf：解析主机 IP，拦截私有/回环/链路本地/保留
   地址；并用带校验的 RedirectHandler 拦截重定向劫持。
"""

import json
import logging
import re
import socket
import time
import ipaddress
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime
from pathlib import Path

from config import settings
from tools.registry import registry

logger = logging.getLogger(__name__)

TIMEOUT = 10  # 所有网络请求统一超时（秒）
MAX_RETRIES = 3  # 最大重试次数（P1: 由 2 提升到 3，配合退避）
BACKOFF_BASE = 0.5  # 重试退避基数（秒）

# 默认 UA（工具类请求）
USER_AGENT = "ai-companion/1.0"
# 浏览器 UA（用于搜索/网页抓取，降低被 403 概率）
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# ==============================
# SSRF / 内网防护
# ==============================

def _is_safe_host(host: str) -> tuple[bool, str]:
    """校验主机是否可安全访问：禁止 localhost、.local、私有/回环/链路本地/保留地址。

    返回 (是否安全, 错误信息)
    """
    host = (host or "").strip().lower()
    if not host:
        return False, "主机名为空"
    if host == "localhost" or host.endswith(".local"):
        return False, f"禁止访问内网主机：{host}"
    # 处理 IPv6 方括号写法
    if host.startswith("[") and "]" in host:
        host = host[1:host.index("]")]
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        return False, f"无法解析主机 {host}：{e}"
    for info in infos:
        ip = info[4][0].split("%")[0]  # 去掉 IPv6 区域标识
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False, f"无效 IP 地址：{ip}"
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
        ):
            return False, f"禁止访问内网/保留地址：{ip}"
        # 显式拦截云元数据端点（169.254.169.254 已在 is_link_local 内，再次明示）
        if str(addr) == "169.254.169.254":
            return False, f"禁止访问云元数据地址：{ip}"
    return True, ""


def _validate_url_ssrf(url: str) -> tuple[bool, str]:
    """URL 级 SSRF 校验：仅允许 http/https，且主机必须解析为安全公网地址。"""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False, f"不支持的协议：{parsed.scheme}，仅支持 http/https"
    if not parsed.hostname:
        return False, f"无效的 URL（无主机名）：{url}"
    return _is_safe_host(parsed.hostname)


class _SsrfRedirectHandler(urllib.request.HTTPRedirectHandler):
    """重定向时再次校验目标，防止通过 302 跳转到内网（重定向劫持 SSRF）。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        ok, err = _validate_url_ssrf(newurl)
        if not ok:
            raise urllib.error.URLError(err)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# 全局安全 opener：重定向经过 SSRF 校验
SAFE_OPENER = urllib.request.build_opener(_SsrfRedirectHandler)


# ==============================
# 统一网络请求封装
# ==============================

def _fetch(
    url: str,
    timeout: int = TIMEOUT,
    retries: int = MAX_RETRIES,
    headers: dict | None = None,
) -> str:
    """统一网络请求封装，带超时、指数退避重试、浏览器 UA 兜底、SSRF 防护。"""
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    last_error = None
    for attempt in range(retries + 1):
        try:
            with SAFE_OPENER.open(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="ignore")
        except (urllib.error.URLError, OSError) as e:
            last_error = e
            if attempt < retries:
                wait = BACKOFF_BASE * (2 ** attempt)
                logger.warning(
                    f"请求失败，{wait:.1f}s 后重试 {attempt + 1}/{retries}: {url} ({e})"
                )
                time.sleep(wait)
    raise last_error or RuntimeError("请求失败")


# ==============================
# 基础工具（原有，未改动逻辑）
# ==============================

@registry.register(name="get_time", description="获取当前日期和时间，无需参数")
def get_time() -> str:
    """返回当前本地时间"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@registry.register(name="calculate", description="执行数学计算，传入表达式如 '1 + 2 * 3'")
def calculate(expression: str) -> str:
    """安全计算数学表达式。

    安全措施：空 builtins（禁止系统调用）+ 空 globals + locals
    这是 Python 官方文档推荐的受限求值方式。
    https://docs.python.org/3/library/functions.html#eval
    """
    try:
        result = eval(expression, {"__builtins__": {}}, {})
        return f"{expression} = {result}"
    except Exception as e:
        return f"计算错误：{e}"


@registry.register(name="echo", description="回显用户输入，用于测试")
def echo(message: str) -> str:
    """原样返回输入内容"""
    return message


# ==============================
# 新增工具
# ==============================

@registry.register(name="get_weather", description="查询指定城市的实时天气信息")
def get_weather(location: str) -> str:
    """查询天气 —— 使用 wttr.in 免费 API（无需 Key）"""
    try:
        safe_loc = urllib.parse.quote(location, safe="")
        url = f"https://wttr.in/{safe_loc}?format=%C+%t+%h+%w&lang=zh"
        raw = _fetch(url).strip()
        if not raw or "Unknown" in raw:
            return f"未找到「{location}」的天气信息"
        return f"{location} 天气：{raw}"
    except Exception as e:
        logger.warning(f"天气查询失败：{e}")
        return f"天气查询失败：暂时无法获取「{location}」的天气信息"


# ==============================
# search_web —— P1 修复：浏览器 UA + 重试 + 双端点回退
# ==============================

def _parse_ddg_html(html: str) -> list[tuple[str, str]]:
    """解析 DuckDuckGo HTML 结果页，返回 [(标题, 真实URL), ...]"""
    results: list[tuple[str, str]] = []
    # 结果标题与跳转链接
    pattern = re.compile(
        r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL
    )
    for m in pattern.finditer(html):
        href = m.group(1)
        title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        # DuckDuckGo 跳转链接形如 /l/?uddg=<encoded_url>，解码出真实地址
        real = href
        uddg = re.search(r"uddg=([^&]+)", href)
        if uddg:
            real = urllib.parse.unquote(uddg.group(1))
        if title:
            results.append((title, real))
    return results


def _search_ddg_html(query: str) -> str:
    """DuckDuckGo HTML 端点（覆盖广，需浏览器 UA）"""
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    headers = {
        "User-Agent": BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    html = _fetch(url, headers=headers)
    results = _parse_ddg_html(html)
    if not results:
        raise ValueError("No results")
    lines = [f"{i}. {t} —— {u}" for i, (t, u) in enumerate(results[:5], 1)]
    return "\n".join(lines)


def _search_ddg_ia(query: str) -> str:
    """DuckDuckGo Instant Answer API（稳定、极少 403，但覆盖窄）"""
    url = f"https://api.duckduckgo.com/?q={urllib.parse.quote(query)}&format=json&no_html=1"
    raw = _fetch(url)
    data = json.loads(raw)
    out = []
    if data.get("AbstractText"):
        out.append(f"摘要：{data['AbstractText']}")
        if data.get("AbstractURL"):
            out.append(f"来源：{data['AbstractURL']}")
    for topic in data.get("RelatedTopics", [])[:3]:
        if "Text" in topic and "FirstURL" in topic:
            out.append(f"- {topic['Text']}（{topic['FirstURL']}）")
    if not out:
        raise ValueError("No results")
    return "\n".join(out)


@registry.register(name="search_web", description="搜索网络信息，返回摘要与结果链接")
def search_web(query: str) -> str:
    """搜索网页 —— 优先 HTML 端点（覆盖广），失败回退 IA 端点。"""
    try:
        return _search_ddg_html(query)
    except Exception as e:
        logger.warning(f"DDG HTML 搜索失败，尝试 IA 端点：{e}")
        try:
            return _search_ddg_ia(query)
        except Exception as e2:
            logger.warning(f"搜索彻底失败：{e2}")
            return f"搜索失败：暂时无法搜索「{query}」"


# ==============================
# get_news —— P1 修复：多源可配置 + 失败回退
# ==============================

# 新闻源：topic -> RSS 地址（P1: 不再写死单一源）
NEWS_SOURCES = {
    "科技": "https://feedx.net/rss/36kr.xml",
    "创业": "https://feedx.net/rss/36kr.xml",
    "数码": "https://sspai.com/feed",
    "综合": "https://feedx.net/rss/36kr.xml",
}


def _parse_feed(raw: str, label: str) -> str:
    """解析 RSS 2.0(<item>) 与 Atom(<entry>)，返回前 5 条标题。

    使用 {*} 通配命名空间，兼容带命名空间的 Atom 源与无命名空间的 RSS 源。
    """
    import xml.etree.ElementTree as ET

    root = ET.fromstring(raw)
    items: list[str] = []
    for item in root.findall(".//{*}item")[:5]:
        title = (item.findtext("{*}title") or "").strip()
        if title:
            items.append(f"- {title}")
    for entry in root.findall(".//{*}entry")[:5]:
        title = (entry.findtext("{*}title") or "").strip()
        if title:
            items.append(f"- {title}")
    if not items:
        raise ValueError("无有效条目")
    return f"【{label}新闻】\n" + "\n".join(items)


@registry.register(
    name="get_news",
    description="获取科技/创业/数码/综合等主题最新新闻；也可直接传入 RSS 源 URL"
)
def get_news(source: str = "科技") -> str:
    """获取新闻 —— 多源 RSS，支持自定义源 URL，失败自动回退 36kr。"""
    # 直接传入 RSS 链接
    if source.startswith("http://") or source.startswith("https://"):
        try:
            return _parse_feed(_fetch(source), "自定义源")
        except Exception as e:
            return f"获取新闻失败：{e}"

    feed_url = NEWS_SOURCES.get(source, NEWS_SOURCES["综合"])
    try:
        return _parse_feed(_fetch(feed_url), source)
    except Exception as e:
        logger.warning(f"新闻源 {source} 获取失败，回退 36kr：{e}")
        try:
            return _parse_feed(_fetch(NEWS_SOURCES["综合"]), "科技(兜底)")
        except Exception as e2:
            return f"获取新闻失败：暂时不可用（{e2}）"


# ==============================
# read_url —— P1 修复：SSRF / 内网防护
# ==============================

@registry.register(
    name="read_url",
    description="读取指定网页的文本内容（截取前1000字），传入完整 URL"
)
def read_url(url: str) -> str:
    """读取网页内容 —— 仅允许 http/https，且屏蔽内网/云元数据（SSRF 防护）。"""
    ok, err = _validate_url_ssrf(url)
    if not ok:
        return err
    try:
        raw = _fetch(
            url,
            headers={
                "User-Agent": BROWSER_UA,
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        # 简单提取文本（不引入 bs4 依赖）
        text = re.sub(r"<script[^>]*>.*?</script>", "", raw, flags=re.DOTALL)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > 1000:
            text = text[:1000] + "..."
        if len(text) < 50:
            return f"无法读取 {url} 的有效内容"
        return text
    except Exception as e:
        logger.warning(f"读取网页失败：{e}")
        return f"读取网页失败：{url} 暂时无法访问"


# ==============================
# 本地文件识别（Phase 4）
# ==============================

# 禁止读取的系统/敏感目录（跨平台）
_FORBIDDEN_DIR_PREFIXES = (
    "/etc", "/proc", "/sys", "/boot", "/dev",
    "C:\\Windows", "C:\\Windows\\", "C:\\Program Files",
    "C:\\ProgramData", "/private/etc", "/System",
)

# 仅允许读取的文本类扩展名（降低误读二进制风险）
_TEXT_EXTS = {
    ".txt", ".md", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml",
    ".yml", ".toml", ".ini", ".cfg", ".conf", ".csv", ".log", ".xml", ".html",
    ".htm", ".css", ".scss", ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd",
    ".rst", ".tex", ".c", ".h", ".cpp", ".hpp", ".go", ".rs", ".java", ".kt",
    ".swift", ".rb", ".php", ".sql", ".r", ".ipynb", ".env.example",
}

_FILE_READ_LIMIT = 200_000  # 单文件读取上限（字符数）


def _resolve_safe_path(path: str) -> tuple[Path | None, str]:
    """校验并解析文件路径：必须在允许根目录内、非系统目录、非越界。

    返回 (解析后的 Path, 错误信息)；成功时 Path 非 None。
    """
    try:
        p = Path(path).resolve()
    except Exception as e:
        return None, f"无效路径：{e}"
    # 越界/系统目录检查
    p_str = str(p)
    for bad in _FORBIDDEN_DIR_PREFIXES:
        if p_str.lower().startswith(bad.lower()):
            return None, f"禁止读取系统目录：{bad}"
    # 根目录白名单
    roots = [Path(r).resolve() for r in settings.FILE_READ_ROOTS]
    if not any(_is_relative_to(p, r) for r in roots):
        allowed = "、".join(str(r) for r in roots)
        return None, f"路径超出允许范围，仅可读取：{allowed}"
    if not p.exists():
        return None, f"文件不存在：{path}"
    if not p.is_file():
        return None, f"不是普通文件：{path}"
    return p, ""


def _is_relative_to(path: Path, root: Path) -> bool:
    """兼容 Python <3.9 的 is_relative_to。"""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


@registry.register(
    name="read_file",
    description="读取本地文本文件内容（如代码、文档、配置）。传入文件绝对路径或相对项目根的路径"
)
def read_file(path: str, limit: int = 0) -> str:
    """读取本地文件 —— 路径白名单 + 文本类型校验 + 大小限制（防任意读/二进制）。"""
    p, err = _resolve_safe_path(path)
    if err:
        return err
    # 扩展名/二进制判断
    if p.suffix.lower() not in _TEXT_EXTS and p.suffix.lower() != "":
        # 允许无扩展名文件，但拒绝已知二进制扩展名
        if p.suffix.lower() in {
            ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico",
            ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
            ".zip", ".gz", ".tar", ".rar", ".7z", ".exe", ".dll", ".so",
            ".mp3", ".mp4", ".avi", ".mov", ".bin",
        }:
            return f"不支持读取二进制文件：{path}（请用图片上传功能识别图片）"
    try:
        text = p.read_text(encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        return f"无法以文本方式读取（可能是二进制文件）：{path}"
    except Exception as e:
        return f"读取失败：{e}"
    cap = limit if limit and limit > 0 else _FILE_READ_LIMIT
    if len(text) > cap:
        text = text[:cap] + f"\n...[已截断，共 {len(text)} 字符]"
    return f"【文件 {p.name}】\n{text}"


@registry.register(
    name="list_directory",
    description="列出本地目录下的文件与子目录。传入目录绝对路径或相对项目根的路径"
)
def list_directory(path: str = ".") -> str:
    """列出目录内容（仅允许根目录内）。"""
    try:
        d = Path(path).resolve()
    except Exception as e:
        return f"无效路径：{e}"
    d_str = str(d)
    for bad in _FORBIDDEN_DIR_PREFIXES:
        if d_str.lower().startswith(bad.lower()):
            return f"禁止访问系统目录：{bad}"
    roots = [Path(r).resolve() for r in settings.FILE_READ_ROOTS]
    if not any(_is_relative_to(d, r) for r in roots):
        allowed = "、".join(str(r) for r in roots)
        return f"路径超出允许范围，仅可读取：{allowed}"
    if not d.exists() or not d.is_dir():
        return f"目录不存在或不是目录：{path}"
    entries = sorted(d.iterdir(), key=lambda x: (x.is_file(), x.name))
    lines = [f"📁 {d}"]
    for e in entries[:100]:
        lines.append(f"  {'📄' if e.is_file() else '📁'} {e.name}")
    if not entries:
        lines.append("  （空目录）")
    return "\n".join(lines)
