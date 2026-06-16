"""技能系统 — 女娲造人 + 内置人物视角"""

from skills.nuwa import Nuwa
from skills.registry import PerspectiveRegistry
from skills.loader import load_all_perspectives

__all__ = ["Nuwa", "PerspectiveRegistry", "load_all_perspectives"]