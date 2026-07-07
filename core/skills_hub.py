"""Skills Hub —— 对齐 Hermes 的用户驱动技能安装（含 quarantine 安全扫描）

Hermes 要点：
- Skills Hub 是用户驱动系统：agent 仅能看到已安装技能，不能自行搜索/安装。
- 源适配器：GitHubSource（Contents API）、SkillsShSource、OptionalSkillSource、UrlSource。
- `.hub/` 元数据：lock.json（溯源 provenance）、quarantine/（待安全扫描）、audit.log（安装历史）。
- 安装前走 quarantine 安全扫描；不安全则隔离，不落盘到 skills/。

本项目本地实现（headless 友好，无需浏览器）：
- 源：LocalDirSource（本地目录）、UrlSource（原始 SKILL.md URL）、GitHubSource（raw 构建）。
- 配置：settings.SKILLS_HUB_SOURCES = JSON 列表，元素形如
  {"type":"local","path":"..."} / {"type":"github","repo":"owner/repo","ref":"main","subdir":"skills"}
  / {"type":"url","url":"https://.../index.json"}。
- ref 前缀快捷方式：url:<http> / github:<owner/repo>/<path> / local:<name>。
- 安装：下载 → security_scan + _guard 扫描 → 安全则 install_remote_skill 落盘并写溯源；
  不安全则隔离到 .hub/quarantine/ 并返回 quarantined。
"""

import datetime
import json
import logging
import urllib.request
import urllib.error
from pathlib import Path

from config import settings
from core.skills_evolution import LEARNED_DIR, _guard, SkillEvolver
from core.security_scan import scan_injection

logger = logging.getLogger(__name__)

HUB_DIR = LEARNED_DIR / ".hub"
LOCK_FILE = HUB_DIR / "lock.json"
QUARANTINE_DIR = HUB_DIR / "quarantine"
AUDIT_LOG = HUB_DIR / "audit.log"


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


class SkillsHub:
    """用户驱动的技能安装中心（含安全隔离）。"""

    def __init__(self):
        HUB_DIR.mkdir(parents=True, exist_ok=True)
        QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
        self._ev = SkillEvolver()

    # ---------- 配置源 ----------
    def _sources(self) -> list[dict]:
        srcs = settings.SKILLS_HUB_SOURCES or []
        if isinstance(srcs, str):
            try:
                srcs = json.loads(srcs)
            except Exception:
                srcs = []
        return srcs or []

    # ---------- 解析 ref ----------
    def _resolve(self, ref: str, source: str | None):
        """返回 (text_or_None, meta) 其中 meta 含来源信息；网络失败返回 (None, {error})。"""
        ref = (ref or "").strip()
        # 显式 source 名称
        if source:
            for s in self._sources():
                if s.get("name") == source or s.get("type") == source:
                    return self._fetch_from_source(s, ref)
        # 前缀快捷方式
        if ref.startswith("url:"):
            return self._fetch_url(ref[len("url:"):].strip())
        if ref.startswith("github:"):
            return self._fetch_github(ref[len("github:"):].strip())
        if ref.startswith("local:"):
            name = ref[len("local:"):].strip()
            for s in self._sources():
                if s.get("type") == "local":
                    return self._fetch_local(s, name)
            return None, {"error": "未配置 local 类型来源（SKILLS_HUB_SOURCES）"}
        # 兜底：当作 URL 直连
        if ref.startswith("http://") or ref.startswith("https://"):
            return self._fetch_url(ref)
        return None, {"error": f"无法解析 ref：{ref}（支持 url:/github:/local: 前缀）"}

    def _fetch_from_source(self, s: dict, ref: str):
        t = s.get("type")
        if t == "local":
            return self._fetch_local(s, ref)
        if t == "github":
            path = ref if ref else s.get("subdir", "")
            return self._fetch_github(f"{s.get('repo','')}/{path}", ref=s.get("ref", "main"))
        if t == "url":
            return self._fetch_url(s.get("url", ""))
        return None, {"error": f"未知来源类型：{t}"}

    def _fetch_local(self, s: dict, name: str):
        base = Path(s.get("path", "")).expanduser()
        if not base.exists():
            return None, {"error": f"本地来源路径不存在：{base}"}
        md = base / name / "SKILL.md"
        if not md.exists():
            return None, {"error": f"本地技能不存在：{md}"}
        try:
            return md.read_text(encoding="utf-8"), {"source": f"local:{base}", "ref": str(md)}
        except Exception as e:
            return None, {"error": f"读取失败：{e}"}

    def _fetch_url(self, url: str):
        url = url.strip()
        if not url:
            return None, {"error": "URL 为空"}
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ai-companion-skills-hub"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.read().decode("utf-8", errors="ignore"), {"source": f"url:{url}", "ref": url}
        except Exception as e:
            return None, {"error": f"下载失败：{e}"}

    def _fetch_github(self, spec: str, ref: str = "main"):
        # spec: "owner/repo/path/to/skill" 或 "owner/repo" + 单独 path
        parts = spec.strip("/").split("/")
        if len(parts) < 2:
            return None, {"error": f"github ref 格式应为 owner/repo[/path]：{spec}"}
        owner, repo = parts[0], parts[1]
        sub = "/".join(parts[2:]) if len(parts) > 2 else ""
        raw = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{sub}/SKILL.md" if sub \
            else f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/SKILL.md"
        return self._fetch_url(raw)

    # ---------- 列出可安装 ----------
    def list_remote(self, source: str | None = None) -> list[dict]:
        out: list[dict] = []
        for s in self._sources():
            if source and source not in (s.get("name"), s.get("type")):
                continue
            t = s.get("type")
            try:
                if t == "local":
                    base = Path(s.get("path", "")).expanduser()
                    if base.exists():
                        for md in sorted(base.glob("*/SKILL.md")):
                            out.append({"name": md.parent.name, "source": f"local:{base}",
                                        "description": "(本地来源)"})
                elif t == "github":
                    # 轻量：仅列出 subdir 下的目录名（不逐个拉取 SKILL.md）
                    api = (f"https://api.github.com/repos/{s.get('repo')}/contents/"
                           f"{s.get('subdir','')}?ref={s.get('ref','main')}")
                    req = urllib.request.Request(api, headers={"User-Agent": "ai-companion"})
                    with urllib.request.urlopen(req, timeout=20) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                    for item in data:
                        if item.get("type") == "dir":
                            out.append({"name": item["name"], "source": f"github:{s.get('repo')}",
                                        "description": f"GitHub {s.get('repo')}"})
                elif t == "url":
                    req = urllib.request.Request(s.get("url", ""), headers={"User-Agent": "ai-companion"})
                    with urllib.request.urlopen(req, timeout=20) as resp:
                        idx = json.loads(resp.read().decode("utf-8"))
                    for it in (idx if isinstance(idx, list) else []):
                        out.append({"name": it.get("name", ""), "source": f"url:{s.get('url')}",
                                    "description": it.get("description", "")})
            except Exception as e:
                logger.warning(f"Hub 来源 {s.get('type')} 列举失败：{e}")
        return out

    # ---------- 安装 ----------
    def install(self, ref: str, source: str | None = None, trust: str = "unverified") -> dict:
        text, meta = self._resolve(ref, source)
        if text is None:
            return {"error": meta.get("error", "解析失败")}
        # 安全扫描（quarantine）
        inj_ok, inj_reasons = scan_injection(text)
        grd_ok, grd_reason = _guard(text)
        if not inj_ok or not grd_ok:
            reasons = inj_reasons + ([grd_reason] if not grd_ok else [])
            return self._quarantine(ref, text, reasons)
        # 解析名称
        name = self._name_from_text(text, ref)
        prov = {
            "source": meta.get("source", source or "unknown"),
            "ref": meta.get("ref", ref),
            "installed_at": _now(),
            "trust": trust,
        }
        res = self._ev.install_remote_skill(name, text, trust=trust, provenance=prov)
        if res.get("error"):
            return res
        self._record_lock(name, prov)
        self._audit(f"install {name} from {prov['source']} trust={trust}")
        return res

    def _name_from_text(self, text: str, ref: str) -> str:
        m = __import__("re").search(r"^name:\s*(.+)$", text, __import__("re").MULTILINE)
        if m:
            return m.group(1).strip()
        # 回退：用 ref 末段
        last = ref.rstrip("/").split("/")[-1].replace(".md", "")
        return last or "hub-skill"

    def _quarantine(self, ref: str, text: str, reasons: list[str]) -> dict:
        slug = self._ev._slugify(self._name_from_text(text, ref))
        qd = QUARANTINE_DIR / slug
        qd.mkdir(parents=True, exist_ok=True)
        (qd / "SKILL.md").write_text(text, encoding="utf-8")
        (qd / "reason.json").write_text(
            json.dumps({"ref": ref, "reasons": reasons, "at": _now()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._audit(f"QUARANTINE {slug}: {', '.join(reasons)}")
        return {"quarantined": True, "name": slug, "reason": "; ".join(reasons)}

    # ---------- 溯源 ----------
    def _record_lock(self, name: str, prov: dict):
        lock: dict = {}
        if LOCK_FILE.exists():
            try:
                lock = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
            except Exception:
                lock = {}
        lock[name] = prov
        LOCK_FILE.write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8")

    def hub_info(self, name: str) -> str:
        if not LOCK_FILE.exists():
            return f"未找到 {name} 的 Hub 溯源信息"
        try:
            lock = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
        except Exception:
            return "溯源文件损坏"
        for key, prov in lock.items():
            if key == name or (prov.get("ref", "").endswith(f"/{name}")):
                return (
                    f"技能《{name}》溯源：\n"
                    f"- 来源：{prov.get('source')}\n"
                    f"- 引用：{prov.get('ref')}\n"
                    f"- 安装时间：{prov.get('installed_at')}\n"
                    f"- 信任级别：{prov.get('trust')}"
                )
        return f"未找到 {name} 的 Hub 溯源信息"

    def _audit(self, line: str):
        try:
            with AUDIT_LOG.open("a", encoding="utf-8") as f:
                f.write(f"{_now()} {line}\n")
        except Exception:
            pass
