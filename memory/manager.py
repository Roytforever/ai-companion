"""记忆管理器 — 负责在对话中自动提取和存储关键信息"""

import re
from memory.store import MemoryStore


class MemoryManager:
    """管理记忆的提取、存储和打包到系统提示"""

    def __init__(self):
        self.store = MemoryStore()
        self._session_prefs_cache: dict[str, str] = {}

    def process_message(self, session_id: str, role: str, content: str):
        """处理一条消息：存储 + 尝试提取偏好"""
        self.store.save_message(session_id, role, content)

        # 如果是用户消息，尝试提取偏好信息
        if role == "user":
            self._extract_preferences(session_id, content)

    def _extract_preferences(self, session_id: str, content: str):
        """从用户消息中提取偏好关键词并存储"""
        patterns = [
            (r"(?:叫[我称]?)\s*(.+)", "nickname"),
            (r"(?:来自|在)\s*(.+?)(?:的|工作|生活|住)", "location"),
            (r"(?:做|是|从事)\s*(.+?)(?:的)?(?:工作|行业|职业)", "job"),
        ]
        for pattern, key in patterns:
            match = re.search(pattern, content)
            if match:
                value = match.group(1).strip()
                self.store.save_pref(f"{session_id}_{key}", value)
                self._session_prefs_cache[f"{session_id}_{key}"] = value

    def get_system_prompt(self, session_id: str) -> str:
        """生成带用户记忆的系统提示"""
        prefs = self.store.get_pref(f"{session_id}_nickname")
        base = "你是一个智能友好的 AI 伴侣，名叫小伴。用中文回答，语气亲切自然。"
        if prefs:
            base += f"\n用户自称：{prefs}"
        return base

    def get_history(self, session_id: str, limit: int = 50) -> list[dict]:
        return self.store.get_history(session_id, limit)

    def clear_history(self, session_id: str):
        self.store.clear_history(session_id)

    def list_sessions(self) -> list[dict]:
        return self.store.list_sessions()