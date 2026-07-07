"""周期自驱（nudge）—— 参照 Hermes 的「周期性自我提示」。

模拟 Hermes 内置的定时提示：主动让自己去
  1) 更新用户画像（跨会话完善）
  2) 把较早对话蒸馏成要点摘要（复用既有蒸馏）
并在 Streamlit 侧通过一个按钮触发，返回人类可读的「我刚做了什么」摘要。
（技能结晶主要在每轮多步任务后自动发生，见 core.skills_evolution。）
"""

import logging
from typing import Optional

from core.skills_evolution import SkillEvolver
from core.user_model import UserModeler
from memory.manager import MemoryManager

logger = logging.getLogger(__name__)


class SelfNudge:
    """一次自我整理：完善画像 + 蒸馏旧记忆。"""

    def __init__(self, memory: MemoryManager):
        self.memory = memory
        self.skills = SkillEvolver()
        self.user = UserModeler()

    def run(self, session_id: str, llm_client=None) -> str:
        if llm_client is None:
            return (
                "⚠️ 当前为离线 / 本地无模型模式，无法执行自我整理。"
                "请切换到联网的云端模型后点击「🧠 自我进化」。"
            )

        steps: list[str] = []

        # 1) 用户画像更新
        history = self.memory.get_history(session_id, limit=20)
        hist_text = "\n".join(f"{m['role']}: {m['content']}" for m in history)
        if self.user.update(hist_text, llm_client=llm_client):
            steps.append("✅ 更新了用户画像（记录了新的稳定偏好）")
        else:
            steps.append("· 用户画像无需更新")

        # 2) 记忆蒸馏（复用既有 maybe_distill）
        digest = self.memory.maybe_distill(session_id, llm_client=llm_client)
        if digest:
            steps.append("📝 把较早的对话蒸馏成了要点摘要")

        # 3) 技能盘点
        n = self.skills.count()
        if n:
            steps.append(f"📚 当前已掌握 {n} 个可复用技能")

        if not steps:
            return "已完成一次自我整理（本轮无变化）。"
        return "🤖 我刚刚做了一次自我整理：\n" + "\n".join(f"- {s}" for s in steps)
