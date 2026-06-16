"""记忆系统 — 存储和检索对话历史与用户偏好"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import settings


class MemoryStore:
    """记忆存储：使用 SQLite 持久化对话和用户信息"""

    def __init__(self):
        self.data_dir = settings.MEMORY_DIR
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "memory.db"
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
                );
                CREATE TABLE IF NOT EXISTS user_prefs (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
                );
                CREATE INDEX IF NOT EXISTS idx_session ON conversations(session_id);
            """)

    def save_message(self, session_id: str, role: str, content: str):
        """保存一条对话消息"""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                "INSERT INTO conversations (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (session_id, role, content, datetime.now().isoformat()),
            )

    def get_history(self, session_id: str, limit: int = 50) -> list[dict]:
        """获取指定会话的最近对话历史"""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT role, content FROM conversations WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def save_pref(self, key: str, value: str):
        """保存用户偏好"""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                """INSERT INTO user_prefs (key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (key, value, datetime.now().isoformat()),
            )

    def get_pref(self, key: str) -> Optional[str]:
        """读取用户偏好"""
        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute(
                "SELECT value FROM user_prefs WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else None

    def clear_history(self, session_id: str):
        """清除指定会话的历史"""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("DELETE FROM conversations WHERE session_id = ?", (session_id,))

    def list_sessions(self) -> list[dict]:
        """列出所有会话"""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT session_id, COUNT(*) as msg_count, MAX(created_at) as last_active
                   FROM conversations GROUP BY session_id ORDER BY last_active DESC"""
            ).fetchall()
        return [dict(r) for r in rows]