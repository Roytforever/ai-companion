"""RL 训练回路 —— 对齐 Hermes/Atropos 的「agent 侧」训练循环（本地可运行部分）

诚实范围说明（重要）：
- Hermes 的真实训练 = Atropos（轨迹 API）+ **Tinker 训练器 + GPU**（GRPO / LoRA）。
  这一重训练需要 TINKER_API_KEY 与显卡，**无法在本地 companion 里跑**。
- 本项目实现的是 **agent 侧回路**（即 Hermes 中由 agent / rl_* 工具负责的部分）：
  轨迹捕获 → 奖励信号 → 偏好对构造 → **本地蒸馏**（把高奖励模式沉淀为技能，
  对应 Hermes 的 learning_graph / learning_mutations / feedback）→ **Atropos 兼容导出**
  （prompts/responses/scores JSONL + DPO 偏好对 + BaseEnv 模板），方便你日后有 GPU 时直接训练。
- 真实 GRPO/LoRA 训练请参考文档：用导出的数据 + `atropos process` / Tinker 训练器。

数据：SQLite 存于 settings.RL_DATA_DIR/trajectories.db。
"""

import json
import logging
import sqlite3
import datetime
from pathlib import Path

from config import settings
from tools.registry import registry
from core.skills_evolution import SKILL_EVOLVER

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


class TrajectoryStore:
    """轨迹存储（SQLite）。"""

    def __init__(self, db_path: Path | None = None):
        self.db = db_path or (settings.RL_DATA_DIR / "trajectories.db")
        self.db.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db), check_same_thread=False)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS trajectories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                ts TEXT,
                prompt TEXT,
                messages_json TEXT,
                tool_calls_json TEXT,
                answer TEXT,
                reward REAL,
                source TEXT
            )"""
        )
        self._conn.commit()

    def add(self, session_id, prompt, messages, tool_calls, answer) -> int:
        cur = self._conn.execute(
            "INSERT INTO trajectories (session_id, ts, prompt, messages_json, "
            "tool_calls_json, answer, reward, source) VALUES (?,?,?,?,?,?,?,?)",
            (
                session_id, _now(), prompt,
                json.dumps(messages, ensure_ascii=False),
                json.dumps(tool_calls, ensure_ascii=False),
                answer, None, "auto",
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def set_reward(self, traj_id: int, reward: float, source: str = "user"):
        self._conn.execute(
            "UPDATE trajectories SET reward=?, source=? WHERE id=?",
            (reward, source, traj_id),
        )
        self._conn.commit()

    def set_reward_last(self, session_id: str, reward: float, source: str = "user"):
        row = self._conn.execute(
            "SELECT id FROM trajectories WHERE session_id=? ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if row:
            self.set_reward(row[0], reward, source)
            return row[0]
        return None

    def all(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id, session_id, ts, prompt, answer, reward, source FROM trajectories "
            "ORDER BY id"
        ).fetchall()
        return [
            {"id": r[0], "session_id": r[1], "ts": r[2], "prompt": r[3],
             "answer": r[4], "reward": r[5], "source": r[6]}
            for r in rows
        ]

    def scored(self) -> list[dict]:
        return [t for t in self.all() if t["reward"] is not None]

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM trajectories").fetchone()[0]


class RLLoop:
    """训练回路控制器（本地可运行部分）。"""

    def __init__(self):
        self.store = TrajectoryStore()

    # ---------- 捕获 ----------
    def capture(self, session_id, prompt, messages, tool_calls, answer) -> int:
        return self.store.add(session_id, prompt, messages, tool_calls, answer)

    # ---------- 奖励 ----------
    def reward(self, traj_id: int, score: float, source: str = "user") -> dict:
        try:
            score = float(score)
        except Exception:
            return {"error": "score 必须是数字"}
        if score < 0 or score > 1:
            return {"error": "score 应在 0..1 之间"}
        self.store.set_reward(int(traj_id), score, source)
        return {"ok": True, "traj_id": int(traj_id), "reward": score}

    def reward_last(self, session_id: str, score: float, source: str = "user") -> dict:
        tid = self.store.set_reward_last(session_id, float(score), source)
        if tid is None:
            return {"error": "该会话暂无轨迹可评分"}
        return {"ok": True, "traj_id": tid, "reward": float(score)}

    def judge(self, traj_id: int, llm_client) -> dict:
        """LLM-as-judge：给一条轨迹的回答质量打 0..1 分。"""
        if llm_client is None:
            return {"error": "未提供 LLM 客户端，无法评分"}
        traj = next((t for t in self.store.all() if t["id"] == traj_id), None)
        if not traj:
            return {"error": "轨迹不存在"}
        prompt = (
            "请作为严格的评审，对下面这个回答的质量打分（仅输出 0 到 1 之间的小数，"
            "1=非常有帮助且正确，0=无帮助或错误）。不要解释。\n\n"
            f"用户问题：{traj['prompt']}\n\n回答：{traj['answer'][:2000]}"
        )
        try:
            resp = llm_client.chat(
                messages=[{"role": "user", "content": prompt}], temperature=0
            )
            txt = (resp.choices[0].message.content or "").strip()
            score = float("".join(c for c in txt if c.isdigit() or c == ".") or "0")
            score = max(0.0, min(1.0, score))
        except Exception as e:
            return {"error": f"评分失败：{e}"}
        self.store.set_reward(traj_id, score, "judge")
        return {"ok": True, "traj_id": traj_id, "reward": score}

    # ---------- 偏好对 ----------
    def build_preferences(self) -> list[dict]:
        """由已评分轨迹构造 (chosen, rejected) 偏好对。

        策略：按奖励排序，最高奖励的回答作为 chosen，最低奖励的作为 rejected
        （仅在两者奖励不同时成对）。
        """
        scored = self.store.scored()
        if len(scored) < 2:
            return []
        ranked = sorted(scored, key=lambda t: t["reward"])
        low, high = ranked[0], ranked[-1]
        if high["reward"] == low["reward"]:
            return []
        return [{
            "chosen": high["answer"],
            "rejected": low["answer"],
            "chosen_reward": high["reward"],
            "rejected_reward": low["reward"],
        }]

    # ---------- 本地蒸馏（自我改进回路） ----------
    def distill(self, llm_client, threshold: float = 0.7) -> dict:
        """分析高奖励轨迹，把可复用模式沉淀为技能（对齐 Hermes learning_graph）。

        返回创建的技能 dict（未触发返回 None）。llm_client 为 None 时直接返回 None。
        """
        high = [t for t in self.store.scored() if t["reward"] >= threshold]
        if not high:
            return None
        # 取奖励最高的若干轨迹摘要，交给 LLM 提炼技能
        summary = "\n\n".join(
            f"【问题】{t['prompt']}\n【回答】{t['answer'][:800]}" for t in high[:5]
        )
        if llm_client is None:
            return None
        prompt = (
            "以下是若干高评分（用户/评审认可）的对话片段，请提炼出一个可复用的技能文档"
            "（SKILL.md），以便未来类似问题直接套用。\n"
            "输出 YAML frontmatter（--- 包裹），含 name/description/version/category/tags，"
            "正文含 ## 适用场景 / ## 步骤 / ## 注意事项。只输出文档本身。\n\n" + summary
        )
        try:
            resp = llm_client.chat(
                messages=[
                    {"role": "system", "content": "你是经验蒸馏助手，输出结构化技能文档。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
            )
            doc = (resp.choices[0].message.content or "").strip()
        except Exception as e:
            logger.warning(f"RL 蒸馏失败：{e}")
            return None
        if not doc:
            return None
        # 复用 SkillEvolver 落盘（含安全守卫 + 去重）
        return SKILL_EVOLVER.maybe_crystallize(
            user_message=high[0]["prompt"],
            assistant_reply=doc,
            tool_calls=1,
            llm_client=llm_client,
            session_id="rl-distill",
        )

    # ---------- Atropos 兼容导出 ----------
    def export_atropos(self, path: str | None = None) -> dict:
        """导出 Atropos 兼容数据：trajectories.jsonl(prompts/responses/scores) +
        preferences.jsonl(DPO 对) + environments/example_env.py(BaseEnv 模板)。

        真实 GRPO/LoRA 训练需 Tinker + GPU，请用此数据交给 atropos process / Tinker 训练器。
        """
        out = Path(path or (settings.RL_DATA_DIR / "export"))
        out.mkdir(parents=True, exist_ok=True)

        scored = self.store.scored()
        traj_path = out / "trajectories.jsonl"
        with traj_path.open("w", encoding="utf-8") as f:
            for t in scored:
                f.write(json.dumps({
                    "prompt": t["prompt"],
                    "response": t["answer"],
                    "score": t["reward"],
                }, ensure_ascii=False) + "\n")

        prefs = self.build_preferences()
        pref_path = out / "preferences.jsonl"
        with pref_path.open("w", encoding="utf-8") as f:
            for p in prefs:
                f.write(json.dumps({
                    "chosen": p["chosen"],
                    "rejected": p["rejected"],
                }, ensure_ascii=False) + "\n")

        env_path = out / "environments" / "example_env.py"
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(_ENV_TEMPLATE, encoding="utf-8")

        return {
            "ok": True,
            "dir": str(out),
            "trajectories": str(traj_path),
            "preferences": str(pref_path),
            "environment_template": str(env_path),
            "n_trajectories": len(scored),
            "n_preferences": len(prefs),
            "note": "真实 GRPO/LoRA 训练需 Tinker + GPU；用 atropos process / Tinker 训练器消费本数据。",
        }

    # ---------- 状态 ----------
    def status(self) -> dict:
        scored = self.store.scored()
        rewards = [t["reward"] for t in scored]
        return {
            "total_trajectories": self.store.count(),
            "scored": len(scored),
            "mean_reward": round(sum(rewards) / len(rewards), 3) if rewards else None,
            "preferences": len(self.build_preferences()),
        }


_ENV_TEMPLATE = '''"""Atropos 环境模板 —— 用于真实 GRPO/LoRA 训练（需 Tinker + GPU）

本文件由 ai-companion 的 RL 回路导出。把它放进 atroposlib 的 environments/ 目录，
按 BaseEnv 实现 load_dataset / get_next_item / score_answer / collect_trajectories，
即可用 `atropos process` 或 Tinker 训练器消费本项目导出的 trajectories.jsonl。

依赖：atroposlib（pip install atroposlib）、训练侧 Tinker/GRPO、GPU。
"""

from atroposlib.envs.base import BaseEnv, BaseEnvConfig


class ExampleEnv(BaseEnv):
    """示例环境：用导出的 (prompt, response, score) 做偏好/打分训练。"""

    name = "ai_companion_example"

    @classmethod
    def config_init(cls) -> tuple[BaseEnvConfig, dict]:
        config = BaseEnvConfig(
            tokenizer_name="Qwen/Qwen3-8B",
            group_size=16,
            max_token_length=8192,
            total_steps=2500,
            lora_rank=32,
            learning_rate=4e-5,
        )
        return config, {}

    def load_dataset(self):
        # 读入 ai-companion 导出的 trajectories.jsonl
        import json
        self.dataset = []
        with open("trajectories.jsonl", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    self.dataset.append(json.loads(line))

    def get_next_item(self):
        import random
        return random.choice(self.dataset)

    def score_answer(self, item, response, **kwargs):
        """分配奖励：用导出的 score，或对 response 自定义评分。"""
        # 这里直接用 dataset 里记录的 score；真实场景应在此写验证器。
        return float(item.get("score", 0.0))

    def collect_trajectories(self, item, response, score, **kwargs):
        return {
            "prompt": item["prompt"],
            "response": response,
            "score": score,
        }
'''


# ==============================
# 注册 RL 工具（capture 由 agent 自动钩子完成）
# ==============================

@registry.register(
    name="rl_status",
    description="查看 RL 训练回路状态：轨迹总数、已评分数、平均奖励、偏好对数量。",
)
def rl_status() -> str:
    return json.dumps(RL_LOOP.status(), ensure_ascii=False)


@registry.register(
    name="rl_reward",
    description="给一条轨迹打分（0..1）。参数 traj_id(整数) + score(0..1)。",
)
def rl_reward(traj_id: int, score: float) -> str:
    return json.dumps(RL_LOOP.reward(traj_id, score), ensure_ascii=False)


@registry.register(
    name="rl_distill",
    description="把高奖励轨迹蒸馏为可复用技能（本地自我改进回路）。需 LLM 客户端（由 agent 注入）。",
)
def rl_distill() -> str:
    # 注意：distill 需要 llm_client；这里通过 agent 持有的单例调用
    from core.agent import _get_agent_llm
    llm = _get_agent_llm()
    r = RL_LOOP.distill(llm)
    if r is None:
        return "未触发蒸馏（无高奖励轨迹或缺少 LLM）"
    return "已蒸馏出新技能：" + (r.get("name") or "unknown")


@registry.register(
    name="rl_export",
    description="导出 Atropos 兼容数据（trajectories.jsonl + preferences.jsonl + 环境模板）。可选 path。",
)
def rl_export(path: str = "") -> str:
    return json.dumps(RL_LOOP.export_atropos(path or None), ensure_ascii=False)


RL_LOOP = RLLoop()
