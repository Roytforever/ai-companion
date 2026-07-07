"""上下文文件优先级链 —— 对齐 Hermes context-files

Hermes 行为：
- 支持的上下文文件：.hermes.md/HERMES.md（最高优先级，递归到 git 根）、AGENTS.md、
  CLAUDE.md、.cursorrules、.cursor/rules/*.mdc；SOUL.md 为全局身份（仅 HERMES_HOME）。
- 优先级：每会话只加载**一种**项目上下文类型（首次匹配生效）：
  .hermes.md → AGENTS.md → CLAUDE.md → .cursorrules。SOUL.md 始终作为身份槽 #1 独立加载。
- 启动期：扫描 cwd，读 UTF-8 → 安全扫描 → >20000 字符截断（头70%/尾20%/标记10%）→ 注入系统提示。
- 渐进式子目录发现：监听工具调用参数中的路径，向上遍历最多 5 个父目录，发现
  AGENTS.md/CLAUDE.md/.cursorrules（每目录首次匹配），安全扫描 → 截断 8000 → 注入工具结果后。
- 安全：所有上下文文件加载前都做提示注入扫描，命中即阻止。

本项目本地实现：
- ContextFileLoader.load_startup()：返回 (project_context, soul) 两段文本，供 agent 注入系统提示。
- SubdirectoryHintTracker.observe(paths)：工具调用后调用，返回需追加到工具结果的上下文文本。
"""

import logging
import os
from pathlib import Path

from config import settings
from core.security_scan import scan_injection

logger = logging.getLogger(__name__)

# 项目上下文优先级（从高到低）；.hermes.md 特殊处理（向上递归到 git 根）
_PROJECT_CHAIN = ["AGENTS.md", "CLAUDE.md", ".cursorrules"]
_TOP_FILE = ".hermes.md"  # 同时识别 HERMES.md

# 截断上限
_STARTUP_LIMIT = 20_000
_STARTUP_HEAD = 0.70
_STARTUP_TAIL = 0.20
_SUBDIR_LIMIT = 8_000


def _truncate(text: str, limit: int, head_ratio: float, tail_ratio: float) -> str:
    if len(text) <= limit:
        return text
    head_n = int(limit * head_ratio)
    tail_n = int(limit * tail_ratio)
    head = text[:head_n]
    tail = text[-tail_n:]
    marker = f"\n\n[...已截断 {os.path.basename('x')}：保留了 {head_n}+{tail_n} / {len(text)} 个字符。请使用文件工具读取完整文件。]"
    return f"{head}\n{marker}\n{tail}"


def _load_and_scan(path: Path, limit: int, head_ratio: float, tail_ratio: float) -> tuple[str | None, str]:
    """读取并安全扫描一个上下文文件。返回 (安全内容, 状态文本)。

    若被扫描拦截，返回 (None, 阻止文本)；若读取失败返回 (None, "")。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None, ""
    ok, reasons = scan_injection(text)
    if not ok:
        blocked = (
            f"[已阻止：{path.name} 包含潜在的提示词注入（{', '.join(reasons)}）。"
            f"内容未加载。]"
        )
        return None, blocked
    return _truncate(text, limit, head_ratio, tail_ratio), ""


class ContextFileLoader:
    """加载项目上下文文件 + SOUL 身份，按 Hermes 优先级链组装。"""

    def __init__(self, cwd: str | None = None):
        self.cwd = Path(cwd or os.getcwd()).resolve()

    # ---------- SOUL.md（身份槽 #1，仅 HERMES_HOME） ----------
    def load_soul(self) -> str:
        soul = settings.HERMES_HOME / "SOUL.md"
        if soul.exists():
            content, _ = _load_and_scan(
                soul, _STARTUP_LIMIT, _STARTUP_HEAD, _STARTUP_TAIL
            )
            if content:
                return f"# 身份（SOUL）\n\n{content}"
        return ""

    # ---------- 项目上下文（启动期，仅一种类型） ----------
    def _find_top_file(self) -> Path | None:
        """向上递归到 git 根 / 文件系统根，找 .hermes.md / HERMES.md。"""
        cur = self.cwd
        last = None
        while True:
            for name in (_TOP_FILE, "HERMES.md"):
                p = cur / name
                if p.exists():
                    return p
            # 到达 git 根则停止向上（.hermes.md 递归到 git 根）
            if (cur / ".git").exists():
                break
            last = cur
            cur = cur.parent
            if cur == last:
                break
        return None

    def load_project_context(self) -> str:
        """按优先级加载一种项目上下文类型（.hermes.md → AGENTS.md → CLAUDE.md → .cursorrules）。"""
        top = self._find_top_file()
        if top is not None:
            content, blocked = _load_and_scan(
                top, _STARTUP_LIMIT, _STARTUP_HEAD, _STARTUP_TAIL
            )
            if content:
                return f"# 项目上下文\n\n## {top.name}\n\n{content}"
            if blocked:
                return f"# 项目上下文\n\n{blocked}"
            return ""
        # 回退到 cwd 下的 AGENTS.md/CLAUDE.md/.cursorrules（首次匹配）
        for name in _PROJECT_CHAIN:
            p = self.cwd / name
            if p.exists():
                content, blocked = _load_and_scan(
                    p, _STARTUP_LIMIT, _STARTUP_HEAD, _STARTUP_TAIL
                )
                if content:
                    return f"# 项目上下文\n\n## {name}\n\n{content}"
                if blocked:
                    return f"# 项目上下文\n\n{blocked}"
        return ""

    def load_startup(self) -> str:
        """返回注入系统提示的上下文块（含 SOUL 身份 + 项目上下文）。"""
        parts = []
        soul = self.load_soul()
        if soul:
            parts.append(soul)
        proj = self.load_project_context()
        if proj:
            parts.append(proj)
        bundle = self.load_bundle()
        if bundle:
            parts.append(bundle)
        return "\n\n".join(parts)

    # ---------- bundle 自动激活的上下文（install_bundle 落盘） ----------
    def load_bundle(self) -> str:
        """读取 BUNDLE_CONTEXT_DIR 下全部 .md，作为自动激活的项目上下文注入。

        由 slash_command 的 install_bundle 把 bundle 的 context/*.md 复制至此；
        每文件经注入扫描，命中即跳过（不阻断其余）。
        """
        d = settings.BUNDLE_CONTEXT_DIR
        if not d.exists():
            return ""
        blocks: list[str] = []
        for p in sorted(d.glob("*.md")):
            content, blocked = _load_and_scan(
                p, _SUBDIR_LIMIT, _STARTUP_HEAD, _STARTUP_TAIL
            )
            if content:
                blocks.append(f"## {p.stem}\n\n{content}")
            elif blocked:
                blocks.append(blocked)
        if not blocks:
            return ""
        return "# 命令包上下文（自动激活）\n\n" + "\n\n".join(blocks)


class SubdirectoryHintTracker:
    """渐进式子目录上下文发现（对齐 Hermes SubdirectoryHintTracker）。"""

    def __init__(self, cwd: str | None = None):
        self.cwd = Path(cwd or os.getcwd()).resolve()
        self._seen: set[str] = set()  # 已发现目录（每目录一次）

    def observe(self, paths: list[str]) -> str:
        """工具调用后调用：从参数路径向上最多 5 层发现上下文文件。

        返回需追加到工具结果的上下文文本（可能为空）。
        """
        discovered: list[str] = []
        for raw in paths or []:
            if not raw:
                continue
            p = Path(raw).expanduser()
            if not p.is_absolute():
                p = self.cwd / p
            p = p.resolve()
            # 若是文件，从父目录开始；向上最多 5 层
            start = p.parent if p.is_file() else p
            cur = start
            for _ in range(6):  # 当前 + 向上 5 层
                key = str(cur)
                if key in self._seen:
                    break
                hit = None
                for name in _PROJECT_CHAIN:
                    cand = cur / name
                    if cand.exists():
                        hit = cand
                        break
                if hit is not None:
                    self._seen.add(key)
                    content, blocked = _load_and_scan(
                        hit, _SUBDIR_LIMIT, _STARTUP_HEAD, _STARTUP_TAIL
                    )
                    if content:
                        discovered.append(f"## {hit.name}（{hit.parent.name}）\n\n{content}")
                    elif blocked:
                        discovered.append(blocked)
                    break
                if cur == cur.parent:
                    break
                cur = cur.parent
        if not discovered:
            return ""
        return "# 项目上下文（渐进发现）\n\n" + "\n\n".join(discovered)
