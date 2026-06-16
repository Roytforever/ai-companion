"""加载所有人物视角"""

from skills.registry import PerspectiveRegistry
from skills.perspectives.builtin import _load_builtins


_registry = PerspectiveRegistry()


def load_all_perspectives() -> PerspectiveRegistry:
    """加载所有内置人物视角"""
    if not _registry.is_ready:
        _load_builtins(_registry)
    return _registry