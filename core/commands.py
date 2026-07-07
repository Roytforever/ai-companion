"""斜杠命令 / 命令包(bundle) —— 对齐 Hermes slash commands & bundles。

命令文件：commands/<name>.md，含可选 YAML frontmatter：
  ---
  description: 一句话描述
  allowed-tools: run_command,read_file
  model: deepseek-chat
  context: ...
  ---
  正文，正文里可用 $ARGUMENTS 或 {{ARGUMENTS}} 占位用户输入。

调用 /name 参数 时，把参数替换占位符，得到展开后的 prompt（作为本轮用户消息）。

bundle：一个目录含 commands/、skills/ 等；install_bundle 把其中 commands/ 拷进项目 commands/。
"""

import re
import json
import shutil
import logging
from pathlib import Path

from config import settings
from tools.registry import registry

logger = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)


class Commands:
    """斜杠命令管理：列 / 取 / 渲染 / 创建 / 安装 bundle。"""

    def __init__(self, commands_dir: Path = None):
        self.dir = Path(commands_dir) if commands_dir else settings.SLASH_COMMANDS_DIR
        self.dir.mkdir(parents=True, exist_ok=True)

    def list(self) -> list[dict]:
        out = []
        for p in sorted(self.dir.glob("*.md")):
            meta, _ = self._parse(p.read_text(encoding="utf-8", errors="ignore"))
            out.append({"name": p.stem, "description": meta.get("description", "")})
        return out

    def _parse(self, text: str):
        m = _FRONTMATTER_RE.match(text)
        if not m:
            return {}, text
        try:
            meta = json.loads(m.group(1))
        except Exception:
            meta = {}
        return meta, m.group(2)

    def get(self, name: str) -> dict | None:
        p = self.dir / f"{name}.md"
        if not p.exists():
            return None
        meta, body = self._parse(p.read_text(encoding="utf-8", errors="ignore"))
        return {"name": name, "meta": meta, "body": body}

    def render(self, name: str, arguments: str = "") -> str | None:
        """展开命令模板，参数替换 $ARGUMENTS / {{ARGUMENTS}}。"""
        c = self.get(name)
        if not c:
            return None
        body = c["body"]
        args = arguments or ""
        body = body.replace("$ARGUMENTS", args).replace("{{ARGUMENTS}}", args)
        return body.strip()

    def create(self, name: str, body: str, description: str = "") -> dict:
        if not name or "/" in name or " " in name:
            return {"ok": False, "error": "非法命令名（不能含空格或 /）"}
        fm = f"---\ndescription: {description or name}\n---\n\n"
        (self.dir / f"{name}.md").write_text(fm + body.strip() + "\n", encoding="utf-8")
        return {"ok": True, "name": name}

    def install_bundle(self, source_dir: str) -> dict:
        """安装命令包：除 commands/ 外，自动激活 skills/ 与 context/。

        - commands/*.md → 拷入项目 commands/（可经 /name 调用）
        - skills/<name>/SKILL.md → 经 SkillEvolver 落盘到 skills/learned/，
          自动进入「已掌握技能」召回（无需手动启用）
        - context/*.md → 拷入 BUNDLE_CONTEXT_DIR，启动时由 ContextFileLoader 自动注入
        """
        from config import settings
        from core.skills_evolution import SKILL_EVOLVER
        from core.security_scan import scan_injection

        src = Path(source_dir)
        if not src.exists():
            return {"ok": False, "error": "源目录不存在"}

        # 1) 斜杠命令
        cmds = 0
        cmd_src = src / "commands"
        if cmd_src.exists():
            for p in cmd_src.glob("*.md"):
                shutil.copy(p, self.dir / p.name)
                cmds += 1

        # 2) 技能：自动激活（落盘到 skills/learned/，随即进入召回）
        skills_installed = []
        skills_failed = 0
        skill_src = src / "skills"
        if skill_src.exists():
            for md in sorted(skill_src.glob("*/SKILL.md")):
                try:
                    text = md.read_text(encoding="utf-8")
                    inj_ok, _ = scan_injection(text)
                    if not inj_ok:
                        skills_failed += 1
                        continue
                    # 以 frontmatter 的 name 作为技能标识（目录名仅作回退），保证 slug 一致
                    fm = re.search(r"^name:\s*(.+)$", text, re.MULTILINE)
                    name = fm.group(1).strip() if fm else md.parent.name
                    res = SKILL_EVOLVER.install_remote_skill(
                        name, text, trust="unverified",
                        provenance={"source": f"bundle:{src}", "ref": str(md)},
                    )
                    if res.get("error"):
                        skills_failed += 1
                    else:
                        skills_installed.append(res.get("name", name))
                except Exception:
                    skills_failed += 1

        # 3) 上下文：自动激活（拷入 BUNDLE_CONTEXT_DIR，启动注入）
        contexts_installed = []
        ctx_src = src / "context"
        if ctx_src.exists():
            settings.BUNDLE_CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
            for p in ctx_src.glob("*.md"):
                try:
                    text = p.read_text(encoding="utf-8")
                    inj_ok, _ = scan_injection(text)
                    if not inj_ok:
                        continue
                    shutil.copy(p, settings.BUNDLE_CONTEXT_DIR / p.name)
                    contexts_installed.append(p.stem)
                except Exception:
                    continue

        return {
            "ok": True,
            "commands_imported": cmds,
            "skills_installed": skills_installed,
            "skills_failed": skills_failed,
            "contexts_installed": contexts_installed,
            "commands_dir": str(self.dir),
            "bundle_context_dir": str(settings.BUNDLE_CONTEXT_DIR),
        }


@registry.register(
    name="slash_command",
    description=(
        "斜杠命令：可复用的自定义指令模板。action=list 列出可用命令；"
        "action=run 展开某命令(name)，将其 body 作为指令（arguments 替换 $ARGUMENTS）；"
        "action=install_bundle 安装命令包(source_dir)：自动导入 commands/、激活 skills/、激活 context/。"
    ),
)
def slash_command(action: str, name: str = "", arguments: str = "", source_dir: str = "") -> str:
    cmds = Commands()
    a = (action or "list").lower()
    if a == "list":
        items = cmds.list()
        if not items:
            return "暂无斜杠命令（在 commands/ 目录放 <name>.md 即可）"
        return "\n".join(f"/{i['name']} —— {i['description']}" for i in items)
    if a == "run":
        if not name:
            return "run 需要提供 name"
        rendered = cmds.render(name, arguments)
        if rendered is None:
            return f"未找到命令：{name}"
        return f"[斜杠命令 /{name} 展开]\n{rendered}"
    if a == "install_bundle":
        if not source_dir:
            return "install_bundle 需要提供 source_dir（bundle 目录路径）"
        r = cmds.install_bundle(source_dir)
        if not r.get("ok"):
            return f"安装失败：{r.get('error')}"
        lines = [
            f"✅ 命令包安装完成：",
            f"- 斜杠命令：{r['commands_imported']} 个 → {r['commands_dir']}",
            f"- 技能自动激活：{len(r['skills_installed'])} 个"
            + (f"（{', '.join(r['skills_installed'])}）" if r['skills_installed'] else ""),
            f"- 上下文自动激活：{len(r['contexts_installed'])} 个"
            + (f"（{', '.join(r['contexts_installed'])}）" if r['contexts_installed'] else ""),
        ]
        if r.get("skills_failed"):
            lines.append(f"⚠️ {r['skills_failed']} 个技能因安全扫描未通过被跳过")
        return "\n".join(lines)
    return f"未知 action：{action}（可选 list / run / install_bundle）"
