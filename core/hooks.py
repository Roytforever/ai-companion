"""事件钩子（Hooks）—— 对齐 Hermes 的三类 Hook 系统中的 Shell/JSON Hook。

Hermes 的 Hooks：在关键生命周期点运行自定义命令 / 注入上下文 / 阻塞工具。
- 事件（取自 Hermes plugin hook 事件子集）：
  session_start / user_message / pre_llm_call / pre_tool_call / post_tool_call /
  assistant_message / error
- 钩子定义：{name, event, matcher(正则, 仅 tool 事件), action, command, timeout}
  - action=command：以 shlex 解析、shell=False 运行 command，stdin 传 JSON，stdout 收 JSON
  - action=inject：直接提供一段文本，注入到下一轮系统提示
- 影响 agent 的方式（对齐 Hermes）：
  - pre_tool_call 返回 {"action":"block"} / {"decision":"block"} → 否决工具调用
  - pre_llm_call / user_message / session_start 返回 {"context": "..."} → 追加到本轮上下文
  - post_* / assistant_message / error 为 fire-and-forget（返回值被忽略）
- 错误隔离：任何 hook 异常都被捕获并跳过，绝不拖垮主循环（对齐 Hermes 非阻塞）。
"""

from __future__ import annotations

import os
import re
import json
import shlex
import logging
import subprocess
from pathlib import Path

from config import settings
from tools.registry import registry
from core.hooks_plugin import load_plugins, get_hooks, reload as reload_plugins_dir

logger = logging.getLogger(__name__)

# 有效事件（与 Hermes plugin hook 事件子集对齐）
VALID_EVENTS = {
    "session_start", "user_message", "pre_llm_call",
    "pre_tool_call", "post_tool_call", "assistant_message", "error",
}
# 可注入上下文的事件（返回值追加到系统提示）
_INJECT_EVENTS = {"session_start", "user_message", "pre_llm_call"}
# 可否决工具的事件
_BLOCK_EVENTS = {"pre_tool_call"}

_HOOKS_FILE = "hooks.json"


class HookManager:
    """钩子管理器：加载、CRUD、在生命周期点触发。"""

    def __init__(self, hooks_dir: Path = None):
        self.dir = Path(hooks_dir) if hooks_dir else settings.HOOKS_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        self._file = self.dir / _HOOKS_FILE
        self._hooks: list[dict] = []
        self._session_context = ""  # session_start 累积的注入
        self.enabled = settings.HOOKS_ENABLED
        self.plugins_dir = settings.HOOKS_PLUGINS_DIR
        self._load()
        # 动态加载 Python 插件钩子（对齐 Hermes Plugin Hooks）
        self._load_plugins()

    # ---- 持久化 ----
    def _load(self):
        if not self._file.exists():
            self._hooks = []
            return
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
            self._hooks = [h for h in data if isinstance(h, dict) and h.get("event") in VALID_EVENTS]
        except Exception as e:
            logger.warning(f"钩子配置解析失败：{e}")
            self._hooks = []

    def _save(self):
        try:
            self._file.write_text(
                json.dumps(self._hooks, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"钩子配置保存失败：{e}")

    # ---- CRUD ----
    def list(self) -> list[dict]:
        return [dict(h) for h in self._hooks]

    def add(self, name: str, event: str, command: str = None, action: str = "command",
            matcher: str = None, timeout: int = 60, enabled: bool = True) -> dict:
        if event not in VALID_EVENTS:
            return {"ok": False, "error": f"无效事件 {event}，可选 {sorted(VALID_EVENTS)}"}
        if action == "command" and not command:
            return {"ok": False, "error": "action=command 时需要 command"}
        hook = {
            "name": name, "event": event, "action": action,
            "command": command, "matcher": matcher, "timeout": timeout,
            "enabled": enabled,
        }
        self._hooks.append(hook)
        self._save()
        return {"ok": True, "hook": hook}

    def remove(self, name: str) -> dict:
        before = len(self._hooks)
        self._hooks = [h for h in self._hooks if h.get("name") != name]
        if len(self._hooks) == before:
            return {"ok": False, "error": "未找到"}
        self._save()
        return {"ok": True}

    def enable(self, name: str, enabled: bool = True) -> dict:
        for h in self._hooks:
            if h.get("name") == name:
                h["enabled"] = enabled
                self._save()
                return {"ok": True}
        return {"ok": False, "error": "未找到"}

    # ---- Python 插件钩子（动态注册） ----
    def _load_plugins(self):
        try:
            load_plugins(self.plugins_dir)
        except Exception as e:
            logger.warning(f"插件钩子加载跳过：{e}")

    def reload_plugins(self) -> int:
        """重新加载 .hooks/plugins 目录下的 Python 插件（热更新）。"""
        try:
            n = reload_plugins_dir(self.plugins_dir)
            logger.info(f"插件热加载完成，新增 {n} 个")
            return n
        except Exception as e:
            logger.warning(f"插件热加载失败：{e}")
            return 0

    def list_plugins(self) -> list[dict]:
        out = []
        for h in get_hooks():
            out.append({
                "name": h["name"], "event": h["event"],
                "matcher": h.get("matcher"),
                "source": Path(str(h.get("source", ""))).name if h.get("source") else "",
            })
        return out

    # ---- 运行 ----
    def _run_command(self, hook: dict, context: dict) -> dict:
        """运行 command：stdin 传 JSON、stdout 解析 JSON 响应。返回响应 dict。"""
        try:
            args = shlex.split(hook["command"])
        except Exception as e:
            logger.warning(f"钩子命令解析失败 {hook['name']}: {e}")
            return {}
        try:
            proc = subprocess.run(
                args, input=json.dumps(context, ensure_ascii=False),
                capture_output=True, text=True, timeout=hook.get("timeout", 60),
            )
        except subprocess.TimeoutExpired:
            logger.warning(f"钩子超时 {hook['name']}")
            return {}
        except Exception as e:
            logger.warning(f"钩子执行异常 {hook['name']}: {e}")
            return {}
        if not proc.stdout.strip():
            return {}
        try:
            return json.loads(proc.stdout)
        except Exception:
            # 非 JSON 输出视为纯文本 context
            return {"context": proc.stdout.strip()}

    def _fire(self, event: str, context: dict) -> dict:
        """触发某事件的全部钩子，返回聚合的 {inject_text, block}。"""
        inject_texts = []
        block = None
        if not self.enabled:
            return {"inject_text": "", "block": None}
        for h in self._hooks:
            if not h.get("enabled", True) or h.get("event") != event:
                continue
            # tool 事件的正则匹配（matcher 为空则匹配全部工具）
            if event in ("pre_tool_call", "post_tool_call"):
                m = h.get("matcher")
                if m and context.get("tool_name"):
                    if not re.search(m, context["tool_name"]):
                        continue
            try:
                if h["action"] == "command":
                    resp = self._run_command(h, context)
                else:
                    resp = {"context": h.get("command") or ""}
                # 阻塞（仅 pre_tool_call 生效）
                if event == "pre_tool_call":
                    if resp.get("action") == "block" or resp.get("decision") == "block":
                        block = resp.get("message") or resp.get("reason") or "被钩子阻塞"
                # 注入上下文
                if event in _INJECT_EVENTS:
                    ctx = resp.get("context") if isinstance(resp, dict) else None
                    if isinstance(ctx, str) and ctx.strip():
                        inject_texts.append(ctx.strip())
                    elif isinstance(resp, str) and resp.strip():
                        inject_texts.append(resp.strip())
            except Exception as e:
                logger.warning(f"钩子 {h['name']} 异常被忽略：{e}")
                continue
        # ---- Python 插件钩子（动态注册，同样支持注入/阻塞） ----
        try:
            for ph in get_hooks():
                if not ph.get("enabled", True) or ph.get("event") != event:
                    continue
                if event in ("pre_tool_call", "post_tool_call"):
                    m = ph.get("matcher")
                    if m and context.get("tool_name"):
                        if not re.search(m, context["tool_name"]):
                            continue
                fn = ph.get("fn")
                if not callable(fn):
                    continue
                resp = fn(context)
                if not resp:
                    continue
                if event == "pre_tool_call":
                    if resp.get("action") == "block" or resp.get("decision") == "block":
                        block = resp.get("message") or resp.get("reason") or "被插件钩子阻塞"
                if event in _INJECT_EVENTS:
                    ctx = resp.get("context") if isinstance(resp, dict) else None
                    if isinstance(ctx, str) and ctx.strip():
                        inject_texts.append(ctx.strip())
                    elif isinstance(resp, str) and resp.strip():
                        inject_texts.append(resp.strip())
        except Exception as e:
            logger.warning(f"插件钩子执行异常被忽略：{e}")
        return {"inject_text": "\n\n".join(inject_texts), "block": block}

    # ---- Agent 调用点 ----
    def session_started(self) -> str:
        """会话启动时触发，返回应常驻注入的上下文（仅 session_start 事件）。"""
        res = self._fire("session_start", {"event": "session_start"})
        self._session_context = res["inject_text"]
        return self._session_context

    def before_llm(self, session_id: str, user_message: str) -> str:
        """返回应注入系统提示的累积文本（session_start + user_message + pre_llm_call）。"""
        parts = []
        if self._session_context:
            parts.append(self._session_context)
        r1 = self._fire("user_message",
                        {"event": "user_message", "session_id": session_id, "message": user_message})
        if r1["inject_text"]:
            parts.append(r1["inject_text"])
        r2 = self._fire("pre_llm_call",
                        {"event": "pre_llm_call", "session_id": session_id, "message": user_message})
        if r2["inject_text"]:
            parts.append(r2["inject_text"])
        return "\n\n".join(p for p in parts if p)

    def pre_tool_call(self, name: str, args: dict) -> tuple[bool, str]:
        """返回 (是否被阻塞, 原因)。由 _run_tool_guarded 调用。"""
        res = self._fire("pre_tool_call",
                         {"event": "pre_tool_call", "tool_name": name, "tool_input": args})
        if res["block"]:
            return True, res["block"]
        return False, ""

    def post_tool_call(self, name: str, args: dict, result: str):
        self._fire("post_tool_call", {
            "event": "post_tool_call", "tool_name": name,
            "tool_input": args, "tool_output": (result or "")[:2000],
        })

    def assistant_message(self, reply: str):
        self._fire("assistant_message", {"event": "assistant_message", "message": reply[:2000]})

    def on_error(self, error: str):
        self._fire("error", {"event": "error", "error": str(error)[:2000]})


# ---- 供 LLM / UI 管理的 hook 工具（对齐 Hermes 配置式 hooks 的「可编程」等价） ----
@registry.register(
    name="hook",
    description=(
        "事件钩子管理：在生命周期点注入上下文 / 运行命令 / 否决工具。"
        "action=list 列出配置式钩子；action=add 新增（event 必填，command 必填当 action=command）；"
        "action=remove/enable/disable 按 name 操作；"
        "action=list_plugins 列出 Python 插件钩子；action=reload_plugins 热重载插件目录。"
    ),
)
def hook_tool(action: str, name: str = "", event: str = "", command: str = "",
              action_mode: str = "command", matcher: str = "", timeout: int = 60) -> str:
    mgr = HookManager()
    a = (action or "list").lower()
    if a == "list":
        items = mgr.list()
        if not items:
            return "暂无配置式钩子（hooks.json 为空）。可放 .py 到 .hooks/plugins/ 注册 Python 插件钩子。"
        return "\n".join(
            f"- {h['name']} [{h['event']}] action={h['action']} "
            f"{('匹配='+h['matcher']) if h.get('matcher') else ''} "
            f"{'启用' if h.get('enabled', True) else '禁用'}"
            for h in items
        )
    if a == "add":
        if not name or not event:
            return "add 需要 name 与 event"
        r = mgr.add(name, event, command=command or None, action=action_mode,
                    matcher=matcher or None, timeout=timeout)
        return f"成功：{r}" if r.get("ok") else f"失败：{r.get('error')}"
    if a == "remove":
        r = mgr.remove(name)
        return "已移除" if r.get("ok") else f"失败：{r.get('error')}"
    if a in ("enable", "disable"):
        r = mgr.enable(name, enabled=(a == "enable"))
        return "已更新" if r.get("ok") else f"失败：{r.get('error')}"
    if a == "list_plugins":
        items = mgr.list_plugins()
        if not items:
            return "暂无 Python 插件钩子（.hooks/plugins/ 为空）"
        return "Python 插件钩子：\n" + "\n".join(
            f"- {h['name']} [{h['event']}] "
            f"{('匹配='+h['matcher']) if h.get('matcher') else ''} 来源={h.get('source','')}"
            for h in items
        )
    if a == "reload_plugins":
        n = mgr.reload_plugins()
        return f"已热重载插件目录，当前共 {n} 个 Python 插件钩子"
    return f"未知 action：{action}（可选 list/add/remove/enable/disable/list_plugins/reload_plugins）"
