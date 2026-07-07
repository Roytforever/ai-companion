"""记忆存储 —— 使用 SQLite 持久化对话、用户偏好与蒸馏摘要，并提供 FTS5 全文召回

为何用 FTS5（而非旧版内存 TF-IDF）：
- 旧版 `search()` 每次调用都重建 TF-IDF 索引（O(N) 重复计算），且无持久化、纯词法无语义。
- 改用 SQLite 内置 FTS5 虚拟表：索引随写入持久化（一次写入、多次检索），支持 SQL 全文检索 +
  bm25 排序；中文用 `trigram` 分词器做子串匹配（零外部依赖，Python 标准库 sqlite3 自带）。
- 仍保持零第三方 ML 依赖（不引入 numpy / sentence-transformers），契合项目「本地精简」定位。
"""

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import settings


class MemoryStore:
    """记忆存储：SQLite 持久化 + FTS5 全文召回"""

    def __init__(self):
        self.data_dir = settings.MEMORY_DIR
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "memory.db"
        self._init_db()
        self._backfill_fts()

    def _detect_fts_tokenizer(self) -> str:
        """探测 SQLite 是否支持 FTS5 trigram 分词器（>=3.34）。不支持则回退 unicode61。"""
        try:
            with sqlite3.connect(":memory:") as probe:
                probe.execute(
                    "CREATE VIRTUAL TABLE _t USING fts5(x, tokenize='trigram')"
                )
            return "trigram"
        except Exception:
            return "unicode61"

    def _init_db(self):
        tokenizer = self._detect_fts_tokenizer()
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.executescript(f"""
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
                CREATE TABLE IF NOT EXISTS memory_digests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    msg_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                    content, tokenize='{tokenizer}'
                );
                CREATE TABLE IF NOT EXISTS memory_fts_meta (
                    rowid INTEGER PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    ref_id INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_session ON conversations(session_id);
                CREATE INDEX IF NOT EXISTS idx_digest_session ON memory_digests(session_id);
                CREATE INDEX IF NOT EXISTS idx_fts_meta_session ON memory_fts_meta(session_id);
            """)

    def _backfill_fts(self):
        """旧库（升级前已存数据）首次启动时回填 FTS 索引，避免历史记忆失联。"""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cnt = conn.execute("SELECT count(*) AS c FROM memory_fts").fetchone()["c"]
            if cnt > 0:
                return
            rows = conn.execute(
                "SELECT id, session_id, content FROM conversations"
            ).fetchall()
            for r in rows:
                cur = conn.execute("INSERT INTO memory_fts(content) VALUES (?)", (r["content"],))
                conn.execute(
                    "INSERT INTO memory_fts_meta(rowid, session_id, kind, ref_id) VALUES (?,?,?,?)",
                    (cur.lastrowid, r["session_id"], "msg", r["id"]),
                )
            drows = conn.execute(
                "SELECT id, session_id, content FROM memory_digests"
            ).fetchall()
            for r in drows:
                cur = conn.execute("INSERT INTO memory_fts(content) VALUES (?)", (r["content"],))
                conn.execute(
                    "INSERT INTO memory_fts_meta(rowid, session_id, kind, ref_id) VALUES (?,?,?,?)",
                    (cur.lastrowid, r["session_id"], "digest", r["id"]),
                )

    # ---------- 对话消息 ----------

    def save_message(self, session_id: str, role: str, content: str):
        """保存一条对话消息，并同步写入 FTS5 全文索引"""
        with sqlite3.connect(str(self.db_path)) as conn:
            cur = conn.execute(
                "INSERT INTO conversations (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (session_id, role, content, datetime.now().isoformat()),
            )
            cid = cur.lastrowid
            cur.execute("INSERT INTO memory_fts(content) VALUES (?)", (content,))
            fid = cur.lastrowid
            conn.execute(
                "INSERT INTO memory_fts_meta(rowid, session_id, kind, ref_id) VALUES (?,?,?,?)",
                (fid, session_id, "msg", cid),
            )

    def get_history(self, session_id: str, limit: int = 50) -> list[dict]:
        """获取指定会话的最近对话历史（按时间正序）"""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT role, content FROM conversations WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def get_all_messages(self, session_id: str) -> list[dict]:
        """获取某会话全部消息（按时间正序），用于蒸馏。"""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, role, content FROM conversations WHERE session_id = ? ORDER BY id ASC",
                (session_id,),
            ).fetchall()
        return [{"id": r["id"], "role": r["role"], "content": r["content"]} for r in rows]

    def count_messages(self, session_id: str) -> int:
        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM conversations WHERE session_id = ?", (session_id,)
            ).fetchone()
        return row[0] if row else 0

    def clear_history(self, session_id: str):
        """清除指定会话的历史与摘要，并同步清理 FTS 索引"""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("DELETE FROM conversations WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM memory_digests WHERE session_id = ?", (session_id,))
            conn.execute(
                "DELETE FROM memory_fts WHERE rowid IN "
                "(SELECT rowid FROM memory_fts_meta WHERE session_id = ?)",
                (session_id,),
            )
            conn.execute("DELETE FROM memory_fts_meta WHERE session_id = ?", (session_id,))

    # ---------- 用户偏好 ----------

    def save_pref(self, key: str, value: str):
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                """INSERT INTO user_prefs (key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (key, value, datetime.now().isoformat()),
            )

    def get_pref(self, key: str) -> Optional[str]:
        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute(
                "SELECT value FROM user_prefs WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else None

    # ---------- 蒸馏摘要 ----------

    def save_digest(self, session_id: str, content: str, msg_count: int):
        with sqlite3.connect(str(self.db_path)) as conn:
            cur = conn.execute(
                "INSERT INTO memory_digests (session_id, content, msg_count, created_at) VALUES (?, ?, ?, ?)",
                (session_id, content, msg_count, datetime.now().isoformat()),
            )
            did = cur.lastrowid
            cur.execute("INSERT INTO memory_fts(content) VALUES (?)", (content,))
            fid = cur.lastrowid
            conn.execute(
                "INSERT INTO memory_fts_meta(rowid, session_id, kind, ref_id) VALUES (?,?,?,?)",
                (fid, session_id, "digest", did),
            )

    def get_digests(self, session_id: str) -> list[str]:
        with sqlite3.connect(str(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT content FROM memory_digests WHERE session_id = ? ORDER BY id DESC",
                (session_id,),
            ).fetchall()
        return [r[0] for r in rows]

    def last_digested_count(self, session_id: str) -> int:
        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute(
                "SELECT MAX(msg_count) FROM memory_digests WHERE session_id = ?", (session_id,)
            ).fetchone()
        return row[0] if row and row[0] is not None else 0

    # ---------- 语义召回（FTS5 全文检索）----------

    def search(self, query: str, session_id: str, top_k: int = 3) -> list[tuple]:
        """在「历史消息 + 蒸馏摘要」中检索与 query 最相关的片段（FTS5 全文 + bm25 排序）。

        返回 [(doc_id, score, text), ...]，doc_id 为 'msg:<id>' 或 'digest:<id>'。
        查询过短（<3 字，trigram 无法生效）时回退 LIKE 子串匹配。
        """
        q = (query or "").strip()
        if not q:
            return []
        if len(q) < 3:
            return self._search_like(q, session_id, top_k)
        try:
            esc = q.replace('"', '""')
            match = f'"{esc}"'
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """SELECT f.content AS content, m.kind AS kind, m.ref_id AS ref_id,
                              bm25(memory_fts) AS rank
                       FROM memory_fts f
                       JOIN memory_fts_meta m ON m.rowid = f.rowid
                       WHERE memory_fts MATCH ? AND m.session_id = ?
                       ORDER BY rank
                       LIMIT ?""",
                    (match, session_id, top_k),
                ).fetchall()
            return [
                (f"{r['kind']}:{r['ref_id']}", -r["rank"], r["content"]) for r in rows
            ]
        except Exception:
            # FTS 异常时保守回退到 LIKE，保证召回不崩
            return self._search_like(q, session_id, top_k)

    def _search_like(self, q: str, session_id: str, top_k: int = 3) -> list[tuple]:
        """短查询 / FTS 异常时的兜底：LIKE 子串匹配 + 命中次数粗排。"""
        like = f"%{q}%"
        docs: list[str] = []
        ids: list[str] = []
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            for r in conn.execute(
                "SELECT id, content FROM conversations WHERE session_id = ? AND content LIKE ?",
                (session_id, like),
            ):
                docs.append(r["content"])
                ids.append(f"msg:{r['id']}")
            for r in conn.execute(
                "SELECT id, content FROM memory_digests WHERE session_id = ? AND content LIKE ?",
                (session_id, like),
            ):
                docs.append(r["content"])
                ids.append(f"digest:{r['id']}")
        scored = []
        ql = q.lower()
        for d, i in zip(docs, ids):
            score = d.lower().count(ql)
            if score > 0:
                scored.append((i, score, d))
        scored.sort(key=lambda x: -x[1])
        return scored[:top_k]

    # ---------- 会话管理 ----------

    def list_sessions(self) -> list[dict]:
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT session_id, COUNT(*) as msg_count, MAX(created_at) as last_active
                   FROM conversations GROUP BY session_id ORDER BY last_active DESC"""
            ).fetchall()
        return [dict(r) for r in rows]
