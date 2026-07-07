"""浏览器自动化工具 —— 对齐 Hermes 的 browser-use / 网页检索能力。

设计（诚实分层，零硬依赖也能跑）：
- 纯读取路径(fetch)：navigate / extract / search 用 urllib 抓取 + html.parser 提取
  可读文本。这是「网页读取」，不依赖任何第三方包。
- Playwright 路径（真实浏览器）：BROWSER_BACKEND=playwright 或 auto 且已安装 playwright 时，
  click / type / screenshot 走真实无头 Chromium；navigate/extract 也会优先用浏览器渲染
  （能拿到 JS 执行后的内容）。会话级复用：懒加载、单进程单 page，支持 navigate→click→type→
  screenshot 连续操作。
- 网络出口 + 潜在副作用：权限系统在 ask 模式下会拦截（见 core/permissions）。

联调：本文件配套 _p1c_verify.py（本地 HTTP 服务 + 真实 Chromium）验证 navigate/extract/
screenshot/click/type 全链路。
"""

import re
import json
import logging
import threading
import urllib.request
import urllib.error
import urllib.parse
from html.parser import HTMLParser

from config import settings
from tools.registry import registry

logger = logging.getLogger(__name__)

_SKIP_TAGS = {"script", "style", "noscript", "head", "meta", "link", "title", "iframe"}
_BR_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section"}


class _TextExtractor(HTMLParser):
    """从 HTML 中抽取可读文本（去脚本/样式，保留换行结构）。"""

    def __init__(self):
        super().__init__()
        self._skip = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
        if tag in _BR_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip > 0:
            self._skip -= 1

    def handle_data(self, data):
        if self._skip == 0:
            t = data.strip()
            if t:
                self.parts.append(t)


def _extract_text(html: str) -> str:
    p = _TextExtractor()
    try:
        p.feed(html)
    except Exception:
        pass
    text = " ".join(p.parts)
    return " ".join(text.split())[:6000]


def _fetch(url: str, timeout: int) -> tuple[str, int, str]:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; AICompanion/1.0)"}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            enc = resp.headers.get_content_charset() or "utf-8"
            return data.decode(enc, errors="ignore"), resp.status, ""
    except urllib.error.HTTPError as e:
        return "", e.code, str(e)
    except Exception as e:
        return "", 0, str(e)


def _use_playwright() -> bool:
    if settings.BROWSER_BACKEND == "fetch":
        return False
    if settings.BROWSER_BACKEND == "playwright":
        return True
    try:
        import importlib.util
        return importlib.util.find_spec("playwright") is not None
    except Exception:
        return False


# ---------- Playwright 持久会话（单进程单 page，懒加载、复用） ----------
_PW_PROC = None
_PW_PAGE = None
_PW_LOCK = threading.Lock()


def _ensure_session():
    """确保 Playwright 会话存在，返回 page。线程安全。"""
    global _PW_PROC, _PW_PAGE
    with _PW_LOCK:
        if _PW_PAGE is not None:
            return _PW_PAGE
        from playwright.sync_api import sync_playwright
        _PW_PROC = sync_playwright().start()
        browser = _PW_PROC.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        _PW_PAGE = browser.new_page()
        return _PW_PAGE


def _close_session():
    """关闭并释放 Playwright 会话（进程退出前调用，可选）。"""
    global _PW_PROC, _PW_PAGE
    with _PW_LOCK:
        if _PW_PAGE is not None:
            try:
                _PW_PAGE.context.close()
            except Exception:
                pass
            _PW_PAGE = None
        if _PW_PROC is not None:
            try:
                _PW_PROC.stop()
            except Exception:
                pass
            _PW_PROC = None


@registry.register(
    name="browser",
    description=(
        "浏览器自动化：navigate/extract 抓取并提取网页可读文本；search 在搜索引擎检索。"
        "click/type 需 selector（真实浏览器，Playwright）；screenshot 保存当前页面截图。"
        "close 关闭浏览器会话。网络出口类操作，ask 权限模式下需授权。"
    ),
)
def browser(action: str, url: str = "", query: str = "", selector: str = "",
           text: str = "") -> str:
    a = (action or "").lower()
    timeout = settings.BROWSER_TIMEOUT

    # ---- 纯读取（无需浏览器） ----
    if a in ("navigate", "extract"):
        # 若 Playwright 可用且指定了具体页面，用浏览器渲染（JS 执行后）
        if a == "navigate" and _use_playwright():
            try:
                page = _ensure_session()
                page.goto(url, timeout=timeout * 1000)
                return f"[URL] {url}\n[可读内容]\n{_extract_text(page.content())[:6000]}"
            except Exception as e:
                logger.warning(f"Playwright navigate 失败，回退 urllib：{e}")
        if a == "extract" and _use_playwright():
            try:
                page = _ensure_session()
                return f"[当前页面] {page.url}\n[可读内容]\n{_extract_text(page.content())[:6000]}"
            except Exception as e:
                logger.warning(f"Playwright extract 失败，回退提示：{e}")
                return "extract 需要先用 navigate 打开页面（Playwright）"
        # 回退：urllib 静态抓取
        if a == "extract":
            return "extract 需要先用 navigate 打开页面（当前为纯读取模式）"
        if not url:
            return "navigate 需要提供 url"
        html, status, err = _fetch(url, timeout)
        if err:
            return f"抓取失败（HTTP {status}）：{err}"
        return f"[URL] {url}\n[可读内容]\n{_extract_text(html)}"

    if a == "search":
        if not query:
            return "需要提供 query"
        target = "https://duckduckgo.com/html/?q=" + urllib.parse.quote(query)
        html, status, err = _fetch(target, timeout)
        if err:
            return f"搜索失败：{err}"
        titles = []
        for m in re.findall(r'result__a"[^>]*>(.*?)</a>', html, re.DOTALL)[:8]:
            clean = re.sub(r"<.*?>", "", m).strip()
            if clean:
                titles.append(clean)
        if not titles:
            return f"[搜索] {query}\n（未能解析结果，返回页面文本前 2000 字）\n{_extract_text(html)[:2000]}"
        return f"[搜索] {query}\n" + "\n".join(f"- {t}" for t in titles)

    # ---- Playwright 真实浏览器动作 ----
    if a in ("click", "type", "screenshot"):
        if not _use_playwright():
            return ("⚠️ 该动作需要 Playwright 真实浏览器。请先 "
                    "`pip install playwright && playwright install chromium`，"
                    "或在 .env 设 BROWSER_BACKEND=fetch 仅用网页读取。")
        try:
            page = _ensure_session()
            if a == "click":
                if not selector:
                    return "click 需要 selector（CSS 选择器）"
                page.click(selector, timeout=timeout * 1000)
                return f"✅ 已点击 {selector}\n[点击后内容]\n{_extract_text(page.content())[:3000]}"
            if a == "type":
                if not selector:
                    return "type 需要 selector（CSS 选择器）"
                page.fill(selector, text or "")
                return f"✅ 已向 {selector} 填入文本：{text!r}"
            if a == "screenshot":
                settings.BROWSER_SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
                out = str(settings.BROWSER_SCREENSHOT_DIR / "screenshot.png")
                page.screenshot(path=out, full_page=False)
                return f"✅ 截图已保存：{out}（页面 {page.url}）"
        except Exception as e:
            return f"浏览器动作失败：{e}"

    if a == "close":
        _close_session()
        return "✅ 已关闭浏览器会话"

    return f"未知 action：{action}（可选 navigate/extract/search/click/type/screenshot/close）"
