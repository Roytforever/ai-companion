"""主动对话引擎 —— 让 AI 伴侣在合适时机主动开启话题，而非被动等问。

设计要点：
- 首访静态问候：UI 首次进入时展示，建立人设与可聊范围。
- 主动搭话：基于「时间段 + 用户记忆」生成一句自然的开场白（离线有模板兜底）。
- 不骚扰：由 UI 控制触发频率（如按钮、或间隔阈值），引擎本身只负责产出内容。
"""

from __future__ import annotations

import datetime
from typing import Optional

from memory.manager import MemoryManager

_GREETING = (
    "嗨，我是你的 AI 伴侣小伴 👋\n"
    "我可以陪你聊天、记事发愁、查天气/新闻/算账，也能切换成某个人的思维视角陪你拆解问题。\n"
    "想聊点什么？或者试试对我说「用鲁迅的视角看看这件事」。"
)

# 不同时间段的模板开场白；{name} 会被用户昵称替换
_TIME_TEMPLATES: dict[str, list[str]] = {
    "morning": [
        "早安{name}～今天有什么想折腾的计划吗？",
        "早上好{name}！昨晚睡得怎么样，今天状态如何？",
        "{name}，新的一天开始了，要不要先梳理下今天的待办？",
    ],
    "afternoon": [
        "下午好{name}，忙了一上午累不累？歇会儿聊两句？",
        "{name}，午后容易走神，要不要我陪你理理手头的事？",
        "嗨{name}，这会儿在忙什么，需要我帮查点什么资料吗？",
    ],
    "evening": [
        "晚上好{name}～今天过得如何，有什么想吐槽或分享的？",
        "{name}，一天快结束了，要不要我帮你记一下明天要做的事？",
        "夜深了{name}，今天有没有什么没聊完的话题，我都在。",
    ],
    "night": [
        "这么晚还没睡呀{name}？注意休息哦，要不要听点轻松的？",
        "{name}，深夜适合放空，有什么心事想跟我说说吗？",
        "夜深了{name}，需要我陪你理理明天的安排再睡吗？",
    ],
}


def _time_slot(now: Optional[datetime.datetime] = None) -> str:
    now = now or datetime.datetime.now()
    h = now.hour
    if 5 <= h < 11:
        return "morning"
    if 11 <= h < 14:
        return "afternoon"
    if 14 <= h < 18:
        return "afternoon"
    if 18 <= h < 23:
        return "evening"
    return "night"


class ProactiveEngine:
    """生成主动对话内容。"""

    def __init__(self, memory: Optional[MemoryManager] = None):
        self.memory = memory or MemoryManager()

    @staticmethod
    def greeting() -> str:
        """首次进入时的静态欢迎语（不依赖模型）。"""
        return _GREETING

    def template_ping(self, session_id: str, now: Optional[datetime.datetime] = None) -> str:
        """离线兜底的主动搭话：时间段模板 + 用户昵称。"""
        slot = _time_slot(now)
        templates = _TIME_TEMPLATES.get(slot, _TIME_TEMPLATES["evening"])
        nickname = self.memory.store.get_pref(f"{session_id}_nickname") or ""
        name = f" {nickname}" if nickname else ""
        # 简单确定性选取，避免每次随机
        idx = (now or datetime.datetime.now()).toordinal() % len(templates)
        return templates[idx].format(name=name)

    def suggest_ping(
        self,
        session_id: str,
        llm_client=None,
        now: Optional[datetime.datetime] = None,
    ) -> str:
        """生成一条主动搭话。

        llm_client 可用时，结合时间段与用户记忆让模型产出更个性化的开场；
        否则回退到模板。
        """
        if llm_client is None:
            return self.template_ping(session_id, now)

        slot = _time_slot(now)
        nickname = self.memory.store.get_pref(f"{session_id}_nickname") or ""
        memory_ctx = self.memory.get_memory_prompt(session_id, "主动聊天 开场", top_k=2)
        prompt = (
            "你是 AI 伴侣小伴。请基于当前时间段和用户背景，生成一句简短自然的主动开场白"
            "（不超过 35 字），像朋友随口搭话，不要问号堆砌、不要列清单。"
            f"\n时间段：{slot}\n用户昵称：{nickname or '未知'}\n用户背景：{memory_ctx}"
        )
        try:
            resp = llm_client.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.8,
                max_tokens=60,
            )
            text = (resp.choices[0].message.content or "").strip()
            return text or self.template_ping(session_id, now)
        except Exception:
            return self.template_ping(session_id, now)
