"""DPO 反馈埋点 —— 收集用户对助手回复的 👍/👎，构造 (prompt, chosen, rejected) 训练对。

用途：
- 每条助手消息下挂 👍/👎 反馈。
- 👍：该回复即 chosen。
- 👎：该回复即 rejected；若用户随后触发「重新生成」，新回复记为 chosen。
- 可导出 JSONL，用于后续 DPO 微调（如配合 Llama-Factory / TRL）。

存储：SQLite（位于 memory/data/feedback.db），与对话记忆同目录，便于管理。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional

from config import settings

_DB_PATH = settings.MEMORY_DIR / "feedback.db"


class FeedbackStore:
    """反馈数据存取与 DPO 对导出。"""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or _DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    assistant_reply TEXT NOT NULL,
                    rating TEXT NOT NULL,            -- 'up' | 'down'
                    regenerated_reply TEXT,          -- 👎 后重新生成的更好回复
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            conn.commit()

    def record(
        self,
        session_id: str,
        prompt: str,
        assistant_reply: str,
        rating: str,  # 'up' | 'down'
        regenerated_reply: Optional[str] = None,
    ) -> int:
        """记录一条反馈，返回记录 id。"""
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO feedback
                    (session_id, prompt, assistant_reply, rating, regenerated_reply)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, prompt, assistant_reply, rating, regenerated_reply),
            )
            conn.commit()
            return cur.lastrowid

    def attach_regeneration(self, feedback_id: int, regenerated_reply: str):
        """为某条 👎 反馈补上重新生成的更好回复（成为 chosen）。"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE feedback SET regenerated_reply=? WHERE id=?",
                (regenerated_reply, feedback_id),
            )
            conn.commit()

    def export_dpo_jsonl(self, out_path: str) -> int:
        """导出为标准 DPO 训练对 JSONL。

        规则：
        - 👍：chosen=assistant_reply，rejected 留空（单侧正样本，可单独用于 SFT/偏好正例）。
        - 👎 且有 regenerated_reply：chosen=regenerated_reply，rejected=assistant_reply（完整 DPO 对）。
        - 👎 无 regenerated_reply：仅记录 rejected（待后续补 chosen）。

        返回导出的有效 DPO 对数量。
        """
        count = 0
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT prompt, assistant_reply, rating, regenerated_reply FROM feedback"
            ).fetchall()

        with open(out_path, "w", encoding="utf-8") as f:
            for prompt, reply, rating, regen in rows:
                if rating == "up":
                    rec = {"prompt": prompt, "chosen": reply, "rejected": None}
                elif regen:
                    rec = {"prompt": prompt, "chosen": regen, "rejected": reply}
                else:
                    rec = {"prompt": prompt, "chosen": None, "rejected": reply}
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                if rec["chosen"] and rec["rejected"]:
                    count += 1
        return count

    def stats(self) -> dict:
        """返回反馈统计。"""
        with sqlite3.connect(self.db_path) as conn:
            up = conn.execute("SELECT COUNT(*) FROM feedback WHERE rating='up'").fetchone()[0]
            down = conn.execute("SELECT COUNT(*) FROM feedback WHERE rating='down'").fetchone()[0]
            regen = conn.execute(
                "SELECT COUNT(*) FROM feedback WHERE regenerated_reply IS NOT NULL"
            ).fetchone()[0]
        return {"up": up, "down": down, "regenerated": regen, "total": up + down}
