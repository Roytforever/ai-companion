"""记忆管理器 — 负责在对话中自动提取和存储关键信息，并支持蒸馏与语义召回"""

import re
from memory.store import MemoryStore


class MemoryManager:
    """管理记忆的提取、存储、蒸馏与检索"""

    def __init__(self):
        self.store = MemoryStore()
        self._session_prefs_cache: dict[str, str] = {}

    def process_message(self, session_id: str, role: str, content: str):
        """处理一条消息：存储 + 尝试提取偏好"""
        self.store.save_message(session_id, role, content)
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
        """生成带用户记忆的基础系统提示"""
        prefs = self.store.get_pref(f"{session_id}_nickname")
        base = "你是一个智能友好的 AI 伴侣，名叫小伴。用中文回答，语气亲切自然。"
        if prefs:
            base += f"\n用户自称：{prefs}"
        return base

    def retrieve_relevant(self, query: str, session_id: str, top_k: int = 3) -> list[str]:
        """语义召回与当前问题相关的历史片段/摘要（向量化 TF-IDF 余弦）。"""
        try:
            results = self.store.search(query, session_id, top_k=top_k)
        except Exception:
            return []
        return [text for (_, _, text) in results]

    # Hermes L1：常驻记忆故意限制长度，逼迫只保留高价值信息（避免无限堆砌）
    RESIDENT_CHAR_BUDGET = 3575

    def get_memory_prompt(self, session_id: str, query: str, top_k: int = 3) -> str:
        """组装注入 LLM 的记忆上下文：用户偏好 + 语义召回的相关片段。

        总长度受 RESIDENT_CHAR_BUDGET 限制——超过则截断尾部召回，
        优先保留头部的用户偏好与高相关片段（对齐 Hermes 的常驻记忆预算）。
        """
        parts = [self.get_system_prompt(session_id)]
        relevant = self.retrieve_relevant(query, session_id, top_k=top_k)
        if relevant:
            snippet = "\n".join(f"- {t}" for t in relevant)
            parts.append(f"\n【相关记忆】（来自历史对话的要点，按需参考）\n{snippet}")
        text = "\n".join(parts)
        if len(text) > self.RESIDENT_CHAR_BUDGET:
            text = text[: self.RESIDENT_CHAR_BUDGET].rstrip() + "\n…（记忆已截断，更多可检索）"
        return text

    def maybe_distill(
        self,
        session_id: str,
        llm_client=None,
        window: int = 6,
        auto_threshold: int = 12,
    ) -> str | None:
        """当历史超出窗口时，把较早的消息蒸馏为要点摘要，降低回传 token。

        返回新生成的摘要文本（若本次未触发蒸馏则返回 None）。
        llm_client 为 None 时跳过（离线/无模型不可用蒸馏）。
        """
        total = self.store.count_messages(session_id)
        if total <= window + auto_threshold:
            return None

        all_msgs = self.store.get_all_messages(session_id)
        old = all_msgs[: max(0, total - window)]
        last_done = self.store.last_digested_count(session_id)
        if len(old) <= last_done:
            return None  # 已蒸馏过，无需重复

        old_text = "\n".join(f"{m['role']}: {m['content']}" for m in old)
        if llm_client is None:
            return None

        prompt = (
            "你是一个记忆整理助手。下面是与用户的对话片段，请提炼为不超过 150 字的"
            "要点摘要，保留：用户的重要个人信息（姓名/职业/所在地/偏好）、未完成任务、"
            "关键决策、情绪状态。只输出摘要本身，不要解释、不要加标题。"
        )
        try:
            resp = llm_client.chat(
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": old_text},
                ],
                temperature=0.3,
            )
            digest = (resp.choices[0].message.content or "").strip()
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"记忆蒸馏失败：{e}")
            return None

        if digest:
            self.store.save_digest(session_id, digest, len(old))
        return digest or None

    def get_history(self, session_id: str, limit: int = 50) -> list[dict]:
        return self.store.get_history(session_id, limit)

    def clear_history(self, session_id: str):
        self.store.clear_history(session_id)

    def list_sessions(self) -> list[dict]:
        return self.store.list_sessions()
