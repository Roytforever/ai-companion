"""知识库 — 蒸馏素材持久化存储

方案说明：见 project.md 中的「知识库方案决策」
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional


# 默认存储路径
DEFAULT_DB_DIR = Path(__file__).parent / "data"


class KnowledgeBase:
    """知识库 — 存储蒸馏素材和对话精华"""

    def __init__(self, db_dir: str | Path = None):
        self.db_dir = Path(db_dir or DEFAULT_DB_DIR)
        self.db_dir.mkdir(parents=True, exist_ok=True)

        # 索引文件
        self.index_path = self.db_dir / "index.json"
        self._index: dict = self._load_index()

    def _load_index(self) -> dict:
        if self.index_path.exists():
            try:
                return json.loads(self.index_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save_index(self):
        self.index_path.write_text(
            json.dumps(self._index, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def save_materials(self, person_name: str, materials: str, source: str = "user") -> str:
        """保存某个人的蒸馏素材"""
        pid = person_name.lower().replace(" ", "-").replace("·", "-")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{pid}_{ts}.json"

        record = {
            "person": person_name,
            "person_id": pid,
            "created_at": ts,
            "source": source,
            "materials": materials,
        }

        path = self.db_dir / filename
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

        # 更新索引
        if pid not in self._index:
            self._index[pid] = {"person": person_name, "entries": []}
        self._index[pid]["entries"].append({
            "filename": filename,
            "created_at": ts,
            "source": source,
        })
        self._save_index()

        return str(path)

    def get_materials(self, person_id: str) -> list[dict]:
        """获取某个人的所有素材"""
        entries = self._index.get(person_id, {}).get("entries", [])
        results = []
        for e in entries:
            path = self.db_dir / e["filename"]
            if path.exists():
                try:
                    results.append(json.loads(path.read_text(encoding="utf-8")))
                except (json.JSONDecodeError, OSError):
                    pass
        return results

    def get_conversation_highlights(self, person_id: str) -> list[str]:
        """获取对话精华（用于后续蒸馏）"""
        materials = self.get_materials(person_id)
        highlights = []
        for m in materials:
            if m.get("source") in ("conversation", "highlight"):
                highlights.append(m.get("materials", ""))
        return highlights

    def list_all_persons(self) -> dict[str, str]:
        """列出所有已存储素材的人物"""
        return {pid: info["person"] for pid, info in self._index.items()}

    def summary(self) -> dict:
        """知识库统计摘要"""
        total_persons = len(self._index)
        total_entries = sum(len(v["entries"]) for v in self._index.values())
        return {
            "total_persons": total_persons,
            "total_entries": total_entries,
            "db_dir": str(self.db_dir),
        }