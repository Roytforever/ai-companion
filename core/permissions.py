"""工具权限审批系统（P0）

对齐 Hermes 的 `tool_guardrails` + 命令审批：在工具真正执行前做一道门。

三种决策：
- ALLOW：直接执行
- DENY ：拒绝执行（返回原因）
- ASK  ：需用户授权（返回「需要授权」提示，不执行）

策略：
- 只读/信息查询类工具（get_time/calculate/read_file/.../MCP 读类）默认放行；
- run_command、skill 写操作、名称含 delete/edit/patch/write/run/create 等工具在 ask 模式下触发 ASK；
- 全局模式 TOOL_PERMISSION_MODE=allow 全部放行，=deny 全部拒绝；
- 会话内授权过的 (工具, 参数) 组合会被缓存，避免重复询问。
"""

import os
import json
import hashlib
import logging

logger = logging.getLogger(__name__)

ALLOW = "allow"
DENY = "deny"
ASK = "ask"

# 默认可直接放行的安全工具（只读/信息查询，无副作用）
SAFE_TOOLS = {
    "get_time", "calculate", "echo", "get_weather", "search_web",
    "get_news", "read_url", "read_file", "list_directory",
}

# skill_manage 的安全动作（仅查看，不需要授权）
_SKILL_SAFE_ACTIONS = {
    "list", "view", "search", "get", "info", "status",
    "hub_list", "hub_info", "trust_status", "trust_set",
}

# 危险动作关键词（命中即需授权）
_DANGEROUS_PREFIXES = (
    "write", "delete", "edit", "patch", "remove", "rm",
    "run", "create", "mkdir", "upload", "move", "mv",
)


def _args_key(tool_name: str, args) -> str:
    try:
        s = json.dumps(args or {}, sort_keys=True, ensure_ascii=False)
    except Exception:
        s = str(args)
    return hashlib.sha1(f"{tool_name}:{s}".encode("utf-8")).hexdigest()[:16]


def _is_dangerous(tool_name: str, args) -> bool:
    """判定某次工具调用是否涉及写/执行副作用。"""
    # run_command 永远视为危险
    if tool_name == "run_command":
        return True
    # browser 涉及网络出口 + 潜在副作用（点击/填表/截图）
    if tool_name == "browser":
        return True
    # skill_manage 按 action 细分
    if tool_name == "skill_manage" and isinstance(args, dict):
        action = (args.get("action") or "").lower()
        if action in _SKILL_SAFE_ACTIONS:
            return False
        return bool(action)  # 任何未列明的安全动作均视为需要授权
    # 其余按工具名关键词判定（MCP 工具 mcp_x_list_resources 等读类不会命中）
    low = tool_name.lower()
    return any(p in low for p in _DANGEROUS_PREFIXES)


class PermissionManager:
    """工具权限管理器。"""

    def __init__(self):
        self.mode = (os.getenv("TOOL_PERMISSION_MODE", "ask") or "ask").lower()
        if self.mode not in (ALLOW, DENY, ASK):
            self.mode = ASK
        self._enabled = os.getenv("TOOL_PERMISSIONS_ENABLED", "true").lower() != "false"
        self._session_allow: set[str] = set()

    def check(self, tool_name: str, args=None) -> tuple[str, str]:
        """返回 (决策, 原因)。"""
        if not self._enabled:
            return ALLOW, "权限系统已关闭"
        # 全局模式优先（DENY 覆盖一切，ALLOW 全部放行）—— 对齐 Hermes 权限语义
        if self.mode == DENY:
            return DENY, "全局拒绝模式（TOOL_PERMISSION_MODE=deny）"
        if self.mode == ALLOW:
            return ALLOW, "全局允许模式（TOOL_PERMISSION_MODE=allow）"
        # ask 模式：安全工具直接放行
        if tool_name in SAFE_TOOLS and not _is_dangerous(tool_name, args):
            return ALLOW, "安全工具"
        # ask 模式：危险调用需授权
        if _is_dangerous(tool_name, args):
            key = _args_key(tool_name, args)
            if key in self._session_allow:
                return ALLOW, "本次会话已授权"
            return ASK, f"工具 `{tool_name}` 涉及写/执行操作，需要授权"
        return ALLOW, "ask 模式下默认放行非危险工具"

    def approve(self, tool_name: str, args=None):
        """记录一次授权（会话级，避免重复询问）。"""
        self._session_allow.add(_args_key(tool_name, args))

    def reset(self):
        self._session_allow.clear()

    @staticmethod
    def is_ask(decision: str) -> bool:
        return decision == ASK
