"""角色扮演系统 — 激活人物视角后修改LLM行为"""

from __future__ import annotations

from typing import Optional

from skills.registry import PerspectiveRegistry
from skills.loader import load_all_perspectives


class Actor:
    """角色系统 — 管理当前激活的人物视角"""

    _instance: Optional["Actor"] = None

    def __new__(cls) -> "Actor":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._active: Optional[str] = None
            cls._instance._registry = None
        return cls._instance

    @property
    def registry(self) -> PerspectiveRegistry:
        if self._registry is None:
            self._registry = load_all_perspectives()
        return self._registry

    def activate(self, perspective_id: str) -> tuple[bool, str]:
        """激活一个人物视角"""
        p = self.registry.get(perspective_id)
        if p is None:
            # 尝试模糊匹配
            found = self.registry.search(perspective_id)
            if found:
                p = found[0]
            else:
                return False, f"未找到人物视角: {perspective_id}"

        self._active = p.id
        return True, f"已切换到「{p.name}」模式"

    def deactivate(self) -> str:
        """退出角色扮演"""
        old = self._active
        self._active = None
        return f"已退出{old}模式，恢复正常回答" if old else "当前没有激活的角色"

    @property
    def active(self) -> Optional[str]:
        return self._active

    def get_system_prompt_override(self) -> Optional[str]:
        """获取当前激活视角的系统提示覆盖"""
        if self._active is None:
            return None

        p = self.registry.get(self._active)
        if p is None:
            return None

        # 如果有完整的skill_content，用它作为系统提示
        if p.skill_content:
            return p.skill_content

        # 否则生成基础的系统提示
        return f"""你正在扮演{p.name}（{p.name_en}）。

角色规则：
1. 用「我」的第一人称回答，不要说「{p.name}会认为...」
2. 直接用{p.name}的语气、节奏、词汇回答问题
3. 基于{p.description}这一核心理念来思考

背景：{p.description}

注意：你是基于公开信息和调研生成的AI角色扮演，不是{p.name_en}本人。
如果用户要求你思考{p.name}从未讨论过的新问题，根据已知的心智模型合理推断，并表明这是推断。"""

    def list_characters(self) -> list[dict]:
        """列出所有可用人物"""
        return [p.to_dict() for p in self.registry.list_all()]

    def search_characters(self, keyword: str) -> list[dict]:
        """搜索人物"""
        return [p.to_dict() for p in self.registry.search(keyword)]