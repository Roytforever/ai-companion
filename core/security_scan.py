"""安全扫描器 —— 提示注入 / 危险内容检测（共享模块）

对齐 Hermes 的上下文文件安全扫描（context-files prompt-injection guard）与
技能安装前的 quarantine 安全扫描。供以下模块复用：
- core/context_files.py：加载 AGENTS.md / SOUL.md / .cursorrules 前扫描
- core/skills_hub.py：从 Hub 安装技能前的 quarantine 扫描
- core/skills_evolution.py：可选复用（其 _guard 仍专注破坏性 shell 模式）

扫描维度（来自 Hermes 文档归纳）：
- 指令覆盖尝试："忽略之前的指示"、"无视你的规则"
- 欺骗模式："不要告诉用户"
- 系统提示词覆盖："系统提示词覆盖"
- 隐藏的 HTML 注释：`<!-- ... -->`
- 隐藏的 div 元素：`<div style="display:none">`
- 凭据窃取：`curl ... $API_KEY` / `wget ... $SECRET`
- 秘密文件访问：`cat .env` / `cat credentials`
- 不可见字符：零宽空格、双向覆盖、词连接符
"""


import re

# 提示注入模式（不区分大小写）
_INJECTION_PATTERNS = [
    (r"忽略(之前|先前|以上|前面的)的?(指示|指令|规则|prompt)", "指令覆盖尝试"),
    (r"无视(你的|之前的)?(规则|指示|指令|约束)", "指令覆盖尝试"),
    (r"ignore\s+(previous|prior|all|above)\s+(instructions|prompts?|rules?)", "指令覆盖尝试(en)"),
    (r"disregard\s+(your|the\s+previous)\s+(instructions|rules?|prompt)", "指令覆盖尝试(en)"),
    (r"不要告诉(用户|user)", "欺骗模式"),
    (r"don'?t\s+tell\s+the\s+user", "欺骗模式(en)"),
    (r"do\s+not\s+tell\s+the\s+user", "欺骗模式(en)"),
    (r"系统提示词覆盖", "系统提示词覆盖"),
    (r"system\s*prompt\s*override", "系统提示词覆盖(en)"),
    (r"<!--.*?-->", "隐藏的 HTML 注释"),
    (r"<div[^>]*style\s*=\s*[\"'][^\"']*display\s*:\s*none", "隐藏的 div 元素(display:none)"),
    (r"curl[^\n]*\$(API_KEY|SECRET|TOKEN|PASSWORD)", "凭据窃取(curl $SECRET)"),
    (r"wget[^\n]*\$(API_KEY|SECRET|TOKEN|PASSWORD)", "凭据窃取(wget $SECRET)"),
    (r"(cat|type|read)\s+(\./)?\.env", "秘密文件访问(.env)"),
    (r"(cat|type|read)\s+[^\n]*(credential|secret|password)", "秘密文件访问(credentials)"),
]

# 不可见/控制字符（零宽空格 U+200B、零宽非连接符 U+200C、零宽连接符 U+200D、
# 双向覆盖 U+202E、词连接符 U+2060、字节序标记 U+FEFF）
_INVISIBLE_RE = re.compile(
    "[\u200b\u200c\u200d\u2060\ufeff\u202a\u202b\u202c\u202d\u202e]"
)


def scan_injection(text: str) -> tuple[bool, list[str]]:
    """扫描文本是否存在提示注入模式。

    返回 (is_safe, reasons)：is_safe=True 表示未发现威胁；reasons 为命中的原因列表。
    """
    if not text:
        return True, []
    reasons: list[str] = []
    for pat, label in _INJECTION_PATTERNS:
        if re.search(pat, text, re.IGNORECASE | re.DOTALL):
            reasons.append(label)
    if _INVISIBLE_RE.search(text):
        reasons.append("包含不可见字符(零宽/双向覆盖等)")
    return (len(reasons) == 0), reasons


def is_safe(text: str) -> bool:
    """便捷方法：文本是否安全（无注入威胁）。"""
    ok, _ = scan_injection(text)
    return ok
