"""人物视角注册中心 — 管理所有内置的思维框架"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Perspective:
    """一个人物视角"""
    id: str                    # 唯一标识, 如 "steve-jobs", "elon-musk"
    name: str                  # 中文名, 如 "乔布斯"
    name_en: str               # 英文名, 如 "Steve Jobs"
    description: str           # 一句话描述
    tags: list[str] = field(default_factory=list)  # 标签
    trigger_words: list[str] = field(default_factory=list)  # 触发词
    skill_content: str = ""    # 完整 SKILL.md 内容
    source: str = "builtin"    # builtin | distilled

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "name_en": self.name_en,
            "description": self.description,
            "tags": self.tags,
            "trigger_words": self.trigger_words,
            "source": self.source,
        }


class PerspectiveRegistry:
    """人物视角注册表 — 单例"""

    _instance: Optional["PerspectiveRegistry"] = None

    def __new__(cls) -> "PerspectiveRegistry":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._perspectives: dict[str, Perspective] = {}
            cls._instance._ready = False
        return cls._instance

    def register(self, p: Perspective) -> None:
        self._perspectives[p.id] = p

    def get(self, pid: str) -> Optional[Perspective]:
        return self._perspectives.get(pid)

    def search(self, keyword: str) -> list[Perspective]:
        """根据关键词搜索匹配的视角"""
        kw = keyword.lower()
        results = []
        for p in self._perspectives.values():
            if (kw in p.id.lower() or kw in p.name or kw in p.name_en.lower()
                    or any(kw in t.lower() for t in p.tags)
                    or any(kw in w.lower() for w in p.trigger_words)):
                results.append(p)
        return results

    def list_all(self) -> list[Perspective]:
        return list(self._perspectives.values())

    def count(self) -> int:
        return len(self._perspectives)

    @property
    def is_ready(self) -> bool:
        return self._ready

    def mark_ready(self) -> None:
        self._ready = True