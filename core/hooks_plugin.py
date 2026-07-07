"""Hooks Python Plugin —— 对齐 Hermes 的 Plugin Hooks（动态注册）。

Hermes 的 Hooks 有两类：
1. 配置式（Shell/JSON）：hooks.json 里写 command / inject（本项目已实现）。
2. Plugin 式（Python）：在 ``plugins/`` 目录放 ``.py`` 文件，用装饰器动态注册
   原生 Python 钩子函数，无需起子进程，可直接读写 agent 状态、访问上下文。

本模块提供：
- ``hook(event, matcher=None, name=None)`` 装饰器：插件文件顶层调用即注册。
- ``load_plugins(plugins_dir)``：扫描目录内 ``.py`` 并 ``import``，触发其中注册。
- ``get_hooks()`` / ``clear_plugins()`` / ``reload(plugins_dir)``：供 HookManager 使用。

插件函数签名：
    def my_hook(payload: dict) -> dict | None:
        ...
        return {"context": "追加到系统提示的文本"}      # 注入（仅注入类事件生效）
        return {"action": "block", "message": "原因"}   # 否决（仅 pre_tool_call 生效）
        return None                                       # 无操作

payload 字段：event / session_id / message / tool_name / tool_input / tool_output。
插件异常被 HookManager 捕获忽略，绝不拖垮主循环（对齐 Hermes 非阻塞）。
"""

import importlib.util
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# 已注册的插件钩子（模块级单例）。每个元素：
# {"name", "event", "matcher", "fn", "enabled", "source"}
_plugin_hooks: list[dict] = []
_loaded = False


def hook(event: str, matcher: str = None, name: str = None):
    """插件钩子注册装饰器。

    用法：
        from core.hooks_plugin import hook

        @hook("pre_tool_call", matcher=r"browser")
        def guard(payload):
            return {"action": "block", "message": "插件禁止浏览器工具"}
    """
    def decorator(fn):
        _plugin_hooks.append({
            "name": name or fn.__name__,
            "event": event,
            "matcher": matcher,
            "fn": fn,
            "enabled": True,
            "source": getattr(fn, "__module__", "plugin"),
        })
        return fn
    return decorator


def clear_plugins():
    """清空已注册的插件钩子（reload 前调用）。"""
    _plugin_hooks.clear()


def _import_plugin_file(path: Path):
    """按文件路径动态导入一个插件模块（执行其顶层 @hook 注册）。"""
    module_name = f"_hermes_plugin_{abs(hash(str(path)))}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, str(path))
        if spec is None or spec.loader is None:
            logger.warning(f"插件加载失败（无法解析）：{path}")
            return
        mod = importlib.util.module_from_spec(spec)
        # 避免污染主模块命名空间
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)
    except Exception as e:
        logger.warning(f"插件导入异常，已忽略 {path}: {e}")


def load_plugins(plugins_dir: Path) -> int:
    """扫描 plugins_dir 内所有 ``.py`` 并导入，返回注册到的钩子数量。

    幂等：调用前先 clear_plugins。出错不抛，单个插件失败不影响其它。
    """
    global _loaded
    clear_plugins()
    d = Path(plugins_dir)
    if not d.exists():
        _loaded = True
        return 0
    count_before = len(_plugin_hooks)
    for p in sorted(d.glob("*.py")):
        if p.name.startswith("__"):
            continue
        _import_plugin_file(p)
    _loaded = True
    return len(_plugin_hooks) - count_before


def reload(plugins_dir: Path) -> int:
    """重新加载插件目录（开发期热更新）。"""
    return load_plugins(plugins_dir)


def get_hooks() -> list[dict]:
    """返回当前所有插件钩子（含 fn，供 HookManager 调用）。"""
    return _plugin_hooks


def count() -> int:
    return len(_plugin_hooks)


def is_loaded() -> bool:
    return _loaded
