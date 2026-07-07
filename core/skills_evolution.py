"""技能进化器 —— 对齐 Hermes 的 skill 模块（程序化记忆 / 自进化核心）

与 Hermes 对齐的要点：
- 单元：每个技能是一个目录 `skills/learned/<slug>/SKILL.md`，frontmatter 含
  name / description / version / category / tags / requires_toolsets（扁平字段，
  便于解析；Hermes 用嵌套 metadata.hermes.*，此处等价简化）。
- 生命周期由 skill_manage 工具驱动：create / patch（首选，令牌高效）/ edit /
  delete / write_file / remove_file。
- 渐进式披露：list_skills()（L0 仅名称+描述+分类）→ view_skill(name)（L1 全文）
  → view_skill(name, path)（L2 引用文件）。
- 条件激活：requires_toolsets 与当前已注册工具匹配才注入。
- 安全守卫：guard_agent_created 启发式扫描危险模式（rm -rf / 凭据外泄等），命中拒绝落盘。
- 自动结晶：maybe_crystallize 把成功多步任务蒸馏为技能（复用 memory 蒸馏与向量召回）。
"""

import datetime
import logging
import re
import shutil
from pathlib import Path
from typing import Optional

from config import settings
from memory.retrieval import TfidfRetriever
from tools.registry import registry

logger = logging.getLogger(__name__)

LEARNED_DIR = Path(__file__).parent.parent / "skills" / "learned"
SIM_THRESHOLD = 0.6  # 去重相似度阈值：超过则视为已有同类技能，跳过
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)

# 守卫：写入技能前的危险模式扫描（对齐 Hermes guard_agent_created 思路）
_DANGER_PATTERNS = [
    r"rm\s+-rf\s+/", r"rm\s+-rf\s+\*", r"format\s+[a-z]:",
    r"sudo\s+rm\s+-rf", r"del\s+/[sq]\s", r":\s*!\s*command",
    r"curl[^\n]*\$(API_KEY|SECRET|TOKEN)", r"wget[^\n]*\$(API_KEY|SECRET|TOKEN)",
    r"shutdown\s+-h", r"mkfs\.",
]


def _today() -> str:
    return datetime.date.today().isoformat()


def _guard(content: str) -> tuple[bool, str]:
    for pat in _DANGER_PATTERNS:
        if re.search(pat, content, re.IGNORECASE):
            return False, f"检测到潜在危险模式（{pat}），已拒绝写入"
    return True, ""


class SkillEvolver:
    """管理「从经验中结晶出的技能」：读取 / 召回 / 结晶 / 自修 / skill_manage。"""

    def __init__(self):
        LEARNED_DIR.mkdir(parents=True, exist_ok=True)
        self._retriever = TfidfRetriever()

    # ---------- 路径 ----------
    def _skill_dir(self, slug: str) -> Path:
        return LEARNED_DIR / slug

    def _skill_md(self, slug: str) -> Path:
        return self._skill_dir(slug) / "SKILL.md"

    # ---------- 解析 ----------
    def _parse(self, path: Path) -> Optional[dict]:
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            return None
        m = _FRONTMATTER_RE.match(text)
        if not m:
            return None
        fm = self._parse_frontmatter(m.group(1))
        body = m.group(2).strip()
        if not body:
            return None
        slug = path.parent.name
        name = fm.get("name") or fm.get("title") or slug
        trust = (fm.get("trust") or "unverified").strip().lower()
        if trust not in ("trusted", "unverified", "quarantined"):
            trust = "unverified"
        return {
            "slug": slug,
            "name": name,
            "title": name,  # 兼容 UI 旧字段
            "description": fm.get("description", ""),
            "version": fm.get("version", "1.0.0"),
            "category": fm.get("category", ""),
            "tags": fm.get("tags", ""),
            "requires_toolsets": fm.get("requires_toolsets", ""),
            "trust": trust,
            "created": fm.get("created", ""),
            "path": str(path),
            "body": body,
        }

    @staticmethod
    def _parse_frontmatter(block: str) -> dict:
        out: dict = {}
        for line in block.splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if ":" in line:
                k, _, v = line.partition(":")
                out[k.strip()] = v.strip()
        return out

    @staticmethod
    def _first_line(content: str) -> str:
        for ln in content.splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#") and not ln.startswith("---"):
                return ln[:80]
        return ""

    # ---------- 读取 / 渐进披露 ----------
    def list_learned(self) -> list[dict]:
        skills = []
        for md in sorted(LEARNED_DIR.glob("*/SKILL.md")):
            meta = self._parse(md)
            if meta:
                skills.append(meta)
        return skills

    def count(self) -> int:
        return len(self.list_learned())

    def list_skills(self) -> list[dict]:
        """L0：仅名称 + 描述 + 分类。"""
        return [
            {"name": s["name"], "description": s["description"], "category": s["category"]}
            for s in self.list_learned()
        ]

    def view_skill(self, name: str, path: Optional[str] = None) -> Optional[dict]:
        """L1：返回技能全文；L2：path 给定时返回该引用文件内容。"""
        for s in self.list_learned():
            if s["name"] == name or s["slug"] == name:
                if path:
                    fp = self._skill_dir(s["slug"]) / path
                    if fp.exists() and fp.is_file():
                        return {
                            "name": s["name"],
                            "path": str(fp),
                            "content": fp.read_text(encoding="utf-8", errors="ignore"),
                        }
                    return None
                return s
        return None

    # ---------- 召回（注入系统提示） ----------
    def _is_active(self, skill: dict) -> bool:
        """条件激活：requires_toolsets 与当前已注册工具匹配才注入。"""
        req = (skill.get("requires_toolsets") or "").strip()
        if not req:
            return True
        req_sets = [r.strip().lower() for r in req.split(",") if r.strip()]
        names = " ".join(t.name for t in registry.list_tools()).lower()
        return any(r in names for r in req_sets)

    # ---------- 信任分级（对齐 Hermes quarantine / auto-run 门控） ----------
    TRUST_LEVELS = ("trusted", "unverified", "quarantined")

    def _trust_level(self, skill: dict) -> str:
        return (skill.get("trust") or "unverified").strip().lower()

    def is_trusted(self, name: str) -> bool:
        s = self.view_skill(name)
        return bool(s) and self._trust_level(s) == "trusted"

    def is_quarantined(self, name: str) -> bool:
        s = self.view_skill(name)
        return bool(s) and self._trust_level(s) == "quarantined"

    def requires_approval(self, name: str) -> bool:
        """未达 trusted 的技能调用前需用户确认（unverified/quarantined 均算）。"""
        s = self.view_skill(name)
        if not s:
            return False
        return self._trust_level(s) != "trusted"

    def set_trust(self, name: str, level: str) -> dict:
        s = self.view_skill(name)
        if not s:
            return {"error": "技能不存在"}
        level = (level or "").strip().lower()
        if level not in self.TRUST_LEVELS:
            return {"error": f"信任级别非法，可选：{', '.join(self.TRUST_LEVELS)}"}
        md = self._skill_dir(s["slug"]) / "SKILL.md"
        text = md.read_text(encoding="utf-8")
        # 覆写 frontmatter 中的 trust 字段
        m = _FRONTMATTER_RE.match(text)
        if not m:
            return {"error": "技能缺少 frontmatter，无法设置信任"}
        fm_block = m.group(1)
        body = m.group(2)
        new_fm_lines = []
        replaced = False
        for line in fm_block.splitlines():
            if line.strip().lower().startswith("trust:"):
                new_fm_lines.append(f"trust: {level}")
                replaced = True
            else:
                new_fm_lines.append(line)
        if not replaced:
            new_fm_lines.append(f"trust: {level}")
        new_text = "---\n" + "\n".join(new_fm_lines) + "\n---\n" + body
        md.write_text(new_text, encoding="utf-8")
        logger.info(f"技能 {name} 信任级别设为 {level}")
        return {"ok": True, "name": name, "trust": level}

    def trust_status(self) -> list[dict]:
        """列出全部技能的信任状态（供 UI / skill_manage 展示）。"""
        out = []
        for s in self.list_learned():
            out.append({
                "name": s["name"],
                "slug": s["slug"],
                "trust": self._trust_level(s),
                "category": s.get("category", ""),
            })
        return out

    def install_remote_skill(
        self, name: str, md_text: str, trust: str = "unverified", provenance: dict | None = None
    ) -> dict:
        """由 SkillsHub 调用：把远程拉取的 SKILL.md 原文落盘（确保含 trust 字段）。

        含安全守卫（_guard）；命中则返回 {"error": ...}。
        """
        ok, reason = _guard(md_text)
        if not ok:
            return {"error": reason}
        # 确保 frontmatter 含 trust
        m = _FRONTMATTER_RE.match(md_text)
        if m and "trust:" not in m.group(1).lower():
            fm_block = m.group(1) + f"\ntrust: {trust}"
            md_text = "---\n" + fm_block + "\n---\n" + m.group(2)
        elif not m:
            md_text = (
                f"---\nname: {name}\ndescription: {name}\nversion: 1.0.0\n"
                f"trust: {trust}\n---\n\n" + md_text.strip() + "\n"
            )
        if provenance:
            prov_line = "\n".join(f"# hub.{k}: {v}" for k, v in provenance.items())
            md_text = md_text + f"\n\n<!-- hub metadata\n{prov_line}\n-->\n"
        slug = self._slugify(name)
        d = self._skill_dir(slug)
        i = 1
        while d.exists():
            d = self._skill_dir(f"{slug}-{i}")
            i += 1
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(md_text, encoding="utf-8")
        meta = self._parse(d / "SKILL.md")
        logger.info(f"Hub 安装技能：{name} -> {d.name} (trust={trust})")
        return meta or {"name": name, "trust": trust}

    def retrieve_relevant(self, query: str, top_k: int = 2) -> str:
        skills = [
            s for s in self.list_learned()
            if self._is_active(s) and self._trust_level(s) != "quarantined"
        ]
        if not skills:
            return ""
        corpus = [f"{s['name']}。{s['description']}\n{s['body']}" for s in skills]
        self._retriever.fit(corpus)
        hits = self._retriever.rank(query, top_k=top_k)
        if not hits:
            return ""
        lines = ["【已掌握的可复用技能】（来自过往经验，若与当前问题相关可直接套用）"]
        for doc_id, _score, _text in hits:
            s = skills[doc_id]
            lines.append(f"- 《{s['name']}》：{s['description']}")
        return "\n".join(lines)

    # ---------- 去重 ----------
    def _too_similar(self, name: str, description: str, body: str) -> Optional[dict]:
        skills = self.list_learned()
        if not skills:
            return None
        corpus = [f"{s['name']}。{s['description']}\n{s['body']}" for s in skills]
        self._retriever.fit(corpus)
        new_doc = f"{name}。{description}\n{body}"
        hits = self._retriever.rank(new_doc, top_k=1)
        if hits and hits[0][1] >= SIM_THRESHOLD:
            return skills[hits[0][0]]
        return None

    # ---------- skill_manage 动作 ----------
    def create_skill(
        self, name: str, content: str, description: str = "",
        category: str = "", tags: str = "", requires_toolsets: str = "",
        trust: str = "unverified",
    ) -> dict:
        ok, reason = _guard(content)
        if not ok:
            logger.warning(f"skill create 守卫拦截：{reason}")
            return {"error": reason}
        slug = self._slugify(name)
        d = self._skill_dir(slug)
        i = 1
        while d.exists():
            d = self._skill_dir(f"{slug}-{i}")
            i += 1
        d.mkdir(parents=True, exist_ok=True)
        desc = description or self._first_line(content) or name
        trust = (trust or "unverified").strip().lower()
        if trust not in self.TRUST_LEVELS:
            trust = "unverified"
        fm = (
            f"---\nname: {name}\ndescription: {desc}\nversion: 1.0.0\n"
            f"category: {category}\ntags: {tags}\nrequires_toolsets: {requires_toolsets}\n"
            f"trust: {trust}\n---\n\n"
        )
        (d / "SKILL.md").write_text(fm + content.strip() + "\n", encoding="utf-8")
        meta = self._parse(d / "SKILL.md")
        logger.info(f"创建技能：{name} -> {d.name} (trust={trust})")
        return meta or {"name": name}

    def patch_skill(self, name: str, old_string: str, new_string: str) -> dict:
        s = self.view_skill(name)
        if not s:
            return {"error": "技能不存在"}
        ok, reason = _guard(new_string)
        if not ok:
            return {"error": reason}
        text = (self._skill_dir(s["slug"]) / "SKILL.md").read_text(encoding="utf-8")
        if old_string not in text:
            return {"error": "old_string 未在技能中匹配"}
        (self._skill_dir(s["slug"]) / "SKILL.md").write_text(
            text.replace(old_string, new_string, 1), encoding="utf-8"
        )
        return {"ok": True, "name": s["name"]}

    def edit_skill(self, name: str, content: str) -> dict:
        s = self.view_skill(name)
        if not s:
            return {"error": "技能不存在"}
        ok, reason = _guard(content)
        if not ok:
            return {"error": reason}
        fm = (
            f"---\nname: {s['name']}\ndescription: {s['description']}\n"
            f"version: {s['version']}\ncategory: {s['category']}\ntags: {s['tags']}\n"
            f"requires_toolsets: {s['requires_toolsets']}\n---\n\n"
        )
        (self._skill_dir(s["slug"]) / "SKILL.md").write_text(
            fm + content.strip() + "\n", encoding="utf-8"
        )
        return {"ok": True, "name": s["name"]}

    def delete_skill(self, name: str) -> dict:
        s = self.view_skill(name)
        if not s:
            return {"error": "技能不存在"}
        shutil.rmtree(self._skill_dir(s["slug"]), ignore_errors=True)
        return {"ok": True, "name": s["name"]}

    def write_file(self, name: str, file_path: str, file_content: str) -> dict:
        s = self.view_skill(name)
        if not s:
            return {"error": "技能不存在"}
        fp = self._skill_dir(s["slug"]) / file_path
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(file_content, encoding="utf-8")
        return {"ok": True, "path": str(fp)}

    def remove_file(self, name: str, file_path: str) -> dict:
        s = self.view_skill(name)
        if not s:
            return {"error": "技能不存在"}
        fp = self._skill_dir(s["slug"]) / file_path
        if fp.exists():
            fp.unlink()
        return {"ok": True}

    # ---------- 结晶 ----------
    def maybe_crystallize(
        self,
        user_message: str,
        assistant_reply: str,
        tool_calls: int,
        llm_client=None,
        session_id: str = "",
    ) -> Optional[dict]:
        """启发式门控 + 去重 + LLM 蒸馏，把一次成功多步任务沉淀为技能。

        返回新技能 dict（未触发/被去重/失败则返回 None）。
        llm_client 为 None 时（离线）直接返回 None。
        """
        if tool_calls < 2:
            return None
        if len(assistant_reply) < 80:
            return None
        if llm_client is None:
            return None

        prompt = (
            "你是一个「经验结晶」助手。用户刚刚借助工具完成了一个任务，请把它沉淀成一个"
            "可复用的技能文档（SKILL.md），以便以后遇到类似问题直接复用。\n\n"
            "要求：\n"
            "1. 输出一个 YAML frontmatter（用 --- 包裹），包含字段："
            "name（技能标识，英文短横线命名）、description（何时使用，用于自动匹配，1-2 句）、"
            "version（1.0.0）、category（分类）、tags（逗号分隔标签）；\n"
            "2. 正文用 Markdown，包含：## 适用场景、## 步骤（有序列表，具体可执行）、## 注意事项；\n"
            "3. 只输出文档本身，不要解释，不要用代码块包裹。\n\n"
            f"用户请求：{user_message}\n\n"
            f"助手回复（含工具结果摘要）：{assistant_reply[:2000]}"
        )
        try:
            resp = llm_client.chat(
                messages=[
                    {"role": "system", "content": "你是经验结晶助手，输出结构化技能文档。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
            )
            doc = (resp.choices[0].message.content or "").strip()
        except Exception as e:
            logger.warning(f"技能结晶失败：{e}")
            return None

        if not doc:
            return None

        m = _FRONTMATTER_RE.match(doc)
        if not m:
            name = self._slugify(user_message[:20]) or "unnamed-skill"
            description = user_message[:60]
            body = doc
        else:
            fm = self._parse_frontmatter(m.group(1))
            name = fm.get("name") or self._slugify(user_message[:20]) or "unnamed-skill"
            description = fm.get("description", "")
            body = m.group(2).strip()

        if self._too_similar(name, description, body):
            return None

        slug = self._slugify(name)
        d = self._skill_dir(slug)
        i = 1
        while d.exists():
            d = self._skill_dir(f"{slug}-{i}")
            i += 1
        d.mkdir(parents=True, exist_ok=True)
        fm = (
            f"---\nname: {name}\ndescription: {description}\nversion: 1.0.0\n"
            f"category: {fm.get('category','') if m else ''}\n"
            f"tags: {fm.get('tags','') if m else ''}\n"
            f"created: {_today()}\nsource_session: {session_id}\ntrust: unverified\n---\n\n"
        )
        (d / "SKILL.md").write_text(fm + body + "\n", encoding="utf-8")
        meta = self._parse(d / "SKILL.md")
        logger.info(f"结晶新技能：{name} -> {d.name}")
        return meta

    # ---------- 自修（局部 patch，不整体覆写） ----------
    def repair(self, slug: str, correction: str) -> bool:
        """发现更优路径或用户纠正时，对技能做局部自修（追加改进记录）。"""
        d = self._skill_dir(slug)
        md = d / "SKILL.md"
        if not md.exists():
            return False
        text = md.read_text(encoding="utf-8")
        addition = f"\n\n## 改进记录\n- {_today()}：{correction.strip()}\n"
        md.write_text(text + addition, encoding="utf-8")
        return True

    @staticmethod
    def _slugify(s: str) -> str:
        s = re.sub(r"[^\w一-鿿]+", "-", s).strip("-")
        s = s[:40]
        return s or "skill"


# ==============================
# 注册 skill_manage 工具（让 LLM 可自主管理技能，对齐 Hermes）
# ==============================

SKILL_EVOLVER = SkillEvolver()


@registry.register(
    name="skill_manage",
    description=(
        "管理可复用技能（程序化记忆）+ 信任分级 + Skills Hub。action 取值：\n"
        "create(新建, 需 name+content+可选 description/category/tags/trust)、\n"
        "patch(局部修改, 需 name+old_string+new_string)、\n"
        "edit(整体重写正文, 需 name+content)、\n"
        "delete(删除, 需 name)、\n"
        "write_file(写支撑文件, 需 name+file_path+file_content)、\n"
        "remove_file(删支撑文件, 需 name+file_path)、\n"
        "trust_set(设信任级别, 需 name+trust[trusted|unverified|quarantined])、\n"
        "trust_status(列出全部技能信任状态, 无需 name)、\n"
        "hub_list(列出 Hub 可安装技能, 可选 source)、\n"
        "hub_install(从 Hub 安装, 需 ref+可选 source/trust)、\n"
        "hub_info(查看已安装技能溯源, 需 name)。"
    ),
)
def skill_manage(
    action: str,
    name: str = "",
    content: str = "",
    description: str = "",
    old_string: str = "",
    new_string: str = "",
    file_path: str = "",
    file_content: str = "",
    category: str = "",
    tags: str = "",
    requires_toolsets: str = "",
    trust: str = "",
    ref: str = "",
    source: str = "",
) -> str:
    """skill_manage 工具实现：分发到 SkillEvolver / SkillsHub 对应动作。"""
    ev = SKILL_EVOLVER
    a = (action or "").strip().lower()
    try:
        if a == "create":
            r = ev.create_skill(
                name, content, description, category, tags, requires_toolsets, trust=trust
            )
        elif a == "patch":
            r = ev.patch_skill(name, old_string, new_string)
        elif a == "edit":
            r = ev.edit_skill(name, content)
        elif a == "delete":
            r = ev.delete_skill(name)
        elif a == "write_file":
            r = ev.write_file(name, file_path, file_content)
        elif a == "remove_file":
            r = ev.remove_file(name, file_path)
        elif a == "trust_set":
            r = ev.set_trust(name, trust)
        elif a == "trust_status":
            rows = ev.trust_status()
            if not rows:
                return "当前没有已掌握的技能"
            lines = ["技能信任状态："]
            for row in rows:
                lines.append(f"- {row['name']} [{row['trust']}] {row.get('category','')}")
            return "\n".join(lines)
        elif a in ("hub_list", "hub_install", "hub_info"):
            from core.skills_hub import SkillsHub
            hub = SkillsHub()
            if a == "hub_list":
                items = hub.list_remote(source or None)
                if not items:
                    return "Hub 暂无可用来源或技能（检查 SKILLS_HUB_SOURCES 配置）"
                return "Hub 可安装技能：\n" + "\n".join(
                    f"- {it['name']} （来源 {it['source']}）{it.get('description','')}" for it in items
                )
            if a == "hub_install":
                if not ref:
                    return "hub_install 需提供 ref（技能引用，如 local:xxx / url:https://... / github:owner/repo/path）"
                r = hub.install(ref, source or None, trust=trust or "unverified")
                if r.get("error"):
                    return f"安装失败：{r['error']}"
                if r.get("quarantined"):
                    return f"已隔离（安全扫描未通过）：{r.get('name')} — {r.get('reason')}"
                return f"已从 Hub 安装：{r.get('name')}（trust={r.get('trust')}）"
            if a == "hub_info":
                info = hub.hub_info(name)
                return info if info else f"未找到 {name} 的 Hub 溯源信息"
        else:
            return (
                "未知动作：%s（可选 create/patch/edit/delete/write_file/remove_file/"
                "trust_set/trust_status/hub_list/hub_install/hub_info）" % action
            )
    except Exception as e:
        return f"skill_manage 执行异常：{e}"
    if r is None:
        return "操作未执行（可能被安全守卫拦截）"
    if r.get("error"):
        return f"失败：{r['error']}"
    return "成功" + (f"：{r.get('name')}" if r.get("name") else "")
