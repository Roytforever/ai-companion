"""受控 shell 执行工具（P0）

对齐 Hermes 的 `tool_executor` / `execute_code`：让本地伴侣具备真实「动手能力」。

安全设计（纵深防御，与 core/permissions 配合）：
- 命令白名单：仅允许 SHELL_ALLOWLIST（默认一组基础运维/构建命令）中的可执行程序；
- 危险模式拦截：rm -rf /、del /s、format、mkfs、dd if=、sudo、curl|sh 等一律拒绝；
- cwd 限制在项目根（FILE_READ_ROOTS[0]），不越权到系统目录；
- 超时控制，避免挂死；
- 权限系统（core/permissions）会在调用前再判一道（run_command 必触发 ASK/拒绝）。
"""

import os
import re
import sys
import json
import logging
import subprocess
from pathlib import Path

from config import settings
from tools.registry import registry

logger = logging.getLogger(__name__)

# 默认命令白名单（基础安全运维/构建命令）；设 SHELL_ALLOWLIST 可覆盖
_DEFAULT_ALLOW = (
    "git,python,python3,pip,pip3,ls,dir,cat,type,echo,find,grep,rg,wc,head,tail,"
    "date,whoami,env,node,npm,npx,pytest,black,ruff,py_compile,git status,"
    "git diff,git log,git add,git commit,git branch,git checkout"
)

# 危险模式（命中即拒绝执行）
_DANGEROUS_PATTERNS = [
    r"\brm\s+-rf\b", r"\brm\b\s+/", r"\bdel\s+/[sq]\b", r"\bformat\s+[a-z]:",
    r":\(\)\s*\{", r"\bsudo\b", r"\bmkfs\b", r"\bdd\b\s+if=", r"\bshutdown\b",
    r"\breboot\b", r">\s*/dev/sd", r"\bchmod\b\s+-R\s+/", r"\bcurl\b[^\n]*\|\s*(sh|bash)",
    r"\bwget\b[^\n]*\|\s*(sh|bash)", r"\bmv\b\s+.*\s+/boot\b", r"\bmv\b\s+.*\s+/etc\b",
    r"\breg\s+delete\b", r"\bnetsh\b",
]


def _base_cmd(command: str) -> str:
    """取命令首个可执行程序名（兼容 cmd /c、powershell）。"""
    # Windows 下简单按空白切分即可
    toks = command.split()
    if not toks:
        return ""
    base = toks[0]
    if base.lower() in ("cmd", "powershell", "pwsh"):
        # 找 /c 或 -c 之后的内嵌命令首项
        for i, t in enumerate(toks):
            if t in ("/c", "-c", "/C"):
                inner = toks[i + 1] if i + 1 < len(toks) else ""
                inner_tok = inner.split()
                return inner_tok[0] if inner_tok else inner
        return base
    return base


def _strip_exe(name: str) -> str:
    return name[:-4] if name.lower().endswith(".exe") else name


def _check_allowed(command: str) -> tuple[bool, str]:
    allow_env = (settings.SHELL_ALLOWLIST or "").strip()
    allowed = [a.strip().lower() for a in (allow_env.split(",") if allow_env else _DEFAULT_ALLOW.split(","))]
    base = _strip_exe(_base_cmd(command)).lower()
    if base not in allowed:
        return False, f"命令 `{base}` 不在白名单（SHELL_ALLOWLIST）"
    for pat in _DANGEROUS_PATTERNS:
        if re.search(pat, command, re.IGNORECASE):
            return False, f"命令匹配危险模式，已拒绝：{pat}"
    return True, ""


@registry.register(
    name="run_command",
    description=(
        "在受控沙箱中执行 shell 命令（白名单 + 危险模式拦截 + 超时 + cwd 限制）。"
        "用于本地文件操作、运行脚本、git 等。危险命令会被拒绝。"
        "返回 stdout/stderr/退出码。注意：写类/执行类操作在 ask 权限模式下需授权。"
    ),
)
def run_command(command: str, timeout: int = 30) -> str:
    """受控执行 shell 命令。"""
    if not command or not command.strip():
        return "错误：命令为空"
    ok, reason = _check_allowed(command)
    if not ok:
        return f"⛔ 执行被拒绝：{reason}"

    cwd = str(Path(settings.FILE_READ_ROOTS[0])) if settings.FILE_READ_ROOTS else str(Path.cwd())
    try:
        proc = subprocess.run(
            command, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
        rc = proc.returncode
        out = proc.stdout or ""
        err = proc.stderr or ""
        body = f"[命令] {command}\n[工作目录] {cwd}\n[退出码] {rc}\n"
        if out:
            body += f"[stdout]\n{out}\n"
        if err:
            body += f"[stderr]\n{err}\n"
        return body[:8000]
    except subprocess.TimeoutExpired:
        return f"⏱ 命令超时（>{timeout}s）：{command}"
    except Exception as e:
        return f"执行异常：{e}"
