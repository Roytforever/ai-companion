"""用户画像建模 —— 对齐 Hermes 第四层「用户画像」+ Honcho 辩证式用户建模。

与旧版（仅追加 `- 事实` 行）不同，本版采用**辩证累积**（dialectical accumulation）：
- 每个维度（traits/preferences/goals/communication_style/expertise）下的每个 facet，
  不是「覆盖式更新」，而是保留 论点(thesis) ↔ 反论点(antithesis) → 综合(synthesis)。
- 新观察与已有结论冲突时：不丢弃旧结论，而是记录为反论点并把矛盾提升为
  「待澄清张力(open_tensions)」，由 reconcile()（LLM）生成综合表述。
- 这样既「越了解你」，又诚实地保留矛盾，避免早期错误观察被固化。

存储：memory/data/user_model.json（结构化）；兼容旧版 user_profile.md（启动时迁移）。
离线（无 LLM）时 observe 跳过，不破坏流程。
"""

import json
import logging
from pathlib import Path
from difflib import SequenceMatcher

from config import settings

logger = logging.getLogger(__name__)

MODEL_PATH = settings.MEMORY_DIR / "user_model.json"
_OLD_PROFILE = settings.MEMORY_DIR / "user_profile.md"

# 维度 → 中文标签
DIMENSIONS = {
    "traits": "性格特质",
    "preferences": "偏好",
    "goals": "目标",
    "communication_style": "沟通风格",
    "expertise": "专业领域",
}

_EMPTY = {k: [] for k in DIMENSIONS}
_EMPTY["open_tensions"] = []


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


class UserModeler:
    """辩证式用户建模：跨会话从对话中蒸馏稳定事实，保留张力而非覆盖。"""

    def __init__(self):
        settings.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        self.data = _EMPTY.copy()
        self._migrate_old()
        self._load()

    # ---- 持久化 ----
    def _load(self):
        if MODEL_PATH.exists():
            try:
                d = json.loads(MODEL_PATH.read_text(encoding="utf-8"))
                for k in _EMPTY:
                    self.data[k] = d.get(k, [])
            except Exception as e:
                logger.warning(f"用户模型加载失败：{e}")

    def _save(self):
        try:
            MODEL_PATH.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"用户模型保存失败：{e}")

    def _migrate_old(self):
        """兼容旧版 user_profile.md：把 `- 事实` 行作为 traits 论点导入一次。"""
        if _OLD_PROFILE.exists() and not MODEL_PATH.exists():
            try:
                lines = [
                    ln.strip()[2:].strip()
                    for ln in _OLD_PROFILE.read_text(encoding="utf-8").splitlines()
                    if ln.strip().startswith("- ")
                ]
                self.data["traits"] = [
                    {"facet": ln[:20], "thesis": ln, "antithesis": "",
                     "synthesis": ln, "confidence": 0.6, "evidence": [ln]}
                    for ln in lines if ln
                ]
                self._save()
            except Exception as e:
                logger.warning(f"旧画像迁移失败：{e}")

    # ---- 观察：LLM 抽取 + 辩证合并 ----
    def observe(self, text: str, llm_client=None) -> bool:
        """从一段对话文本中蒸馏稳定事实并辩证合并。返回是否有变更。"""
        if llm_client is None:
            return False
        if not text or not text.strip():
            return False
        current = self.format_for_prompt()
        prompt = (
            "你是用户建模器。分析下面这段对话，提取其中「稳定、跨会话有用」的用户事实，"
            "例如：性格特质、偏好、目标、沟通风格、专业领域。\n"
            "只输出 JSON（不要其它文字），格式：\n"
            '[{"dimension":"traits|preferences|goals|communication_style|expertise",'
            '"facet":"该事实的维度名(短)","claim":"具体结论","confidence":0.0~1.0}]\n'
            "不要重复下面已有的画像；不确定的不要编造；最多 8 条。\n\n"
            f"已有画像：\n{current}\n\n近期对话：\n{text[:3000]}"
        )
        try:
            resp = llm_client.chat(
                messages=[
                    {"role": "system", "content": "只输出 JSON 数组。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
            )
            raw = (resp.choices[0].message.content or "").strip()
        except Exception as e:
            logger.warning(f"用户观察抽取失败：{e}")
            return False
        facts = self._parse_facts(raw)
        if not facts:
            return False
        changed = False
        for f in facts:
            if self._merge(f):
                changed = True
        if changed:
            self._save()
        return changed

    @staticmethod
    def _parse_facts(raw: str) -> list[dict]:
        try:
            # 容忍 ```json 围栏
            if "```" in raw:
                raw = raw.split("```")[1].split("```")[0]
            data = json.loads(raw)
            if isinstance(data, dict):
                data = [data]
            return [d for d in data if isinstance(d, dict) and d.get("dimension") in DIMENSIONS]
        except Exception:
            return []

    def _merge(self, fact: dict) -> bool:
        """把一条 fact 辩证合并进对应维度。返回是否变更。"""
        dim = fact["dimension"]
        facet = (fact.get("facet") or "").strip()
        claim = (fact.get("claim") or "").strip()
        conf = max(0.0, min(1.0, float(fact.get("confidence", 0.6) or 0.6)))
        if not facet or not claim:
            return False
        arr = self.data[dim]
        # 找同 facet 的已有条目（facet 相似或 claim 相似）
        for item in arr:
            if _similar(item["facet"], facet) > 0.6 or _similar(item["synthesis"], claim) > 0.7:
                # 与现有结论一致 → 提升置信、补证据
                if _similar(item["synthesis"], claim) > 0.6:
                    item["confidence"] = min(1.0, item["confidence"] + 0.1)
                    item.setdefault("evidence", []).append(claim)
                    return True
                # 冲突 → 记为反论点 + 张力，待 reconcile 综合
                item["antithesis"] = claim
                item["needs_synthesis"] = True
                item["confidence"] = max(0.3, item["confidence"] - 0.2)
                item.setdefault("evidence", []).append(claim)
                tension = f"{dim}.{facet}：{item['thesis']} ↔ {claim}"
                if tension not in self.data["open_tensions"]:
                    self.data["open_tensions"].append(tension)
                return True
        # 新 facet
        arr.append({
            "facet": facet, "thesis": claim, "antithesis": "",
            "synthesis": claim, "confidence": conf,
            "needs_synthesis": False, "evidence": [claim],
        })
        return True

    def reconcile(self, llm_client=None) -> int:
        """对存在张力的条目，用 LLM 生成辩证综合(synthesis)。返回综合条数。"""
        if llm_client is None:
            return 0
        done = 0
        for dim in DIMENSIONS:
            for item in self.data[dim]:
                if item.get("needs_synthesis") and item.get("antithesis"):
                    syn = self._synthesize(item, llm_client)
                    if syn:
                        item["synthesis"] = syn
                        item["needs_synthesis"] = False
                        done += 1
        if done:
            self._save()
        return done

    @staticmethod
    def _synthesize(item: dict, llm_client) -> str:
        prompt = (
            "你是辩证综合者。针对同一用户特质出现的两种相反观察，给出一句兼顾二者的综合表述"
            "（简洁、不空洞）。\n"
            f"观察A：{item.get('thesis','')}\n"
            f"观察B：{item.get('antithesis','')}\n"
            "综合："
        )
        try:
            resp = llm_client.chat(
                messages=[{"role": "user", "content": prompt}], temperature=0.3
            )
            return (resp.choices[0].message.content or "").strip().strip("综合：").strip()
        except Exception:
            return ""

    # ---- 输出 ----
    def format_for_prompt(self) -> str:
        """生成注入系统提示的关系型画像文本。"""
        lines = []
        for dim, label in DIMENSIONS.items():
            for item in self.data[dim]:
                syn = item.get("synthesis") or item.get("thesis") or ""
                conf = item.get("confidence", 0.5)
                if item.get("antithesis"):
                    lines.append(
                        f"- {label}/{item['facet']}：综合「{syn}」"
                        f"（张力：{item['thesis']} ↔ {item['antithesis']}，置信{conf:.0%}）"
                    )
                else:
                    lines.append(f"- {label}/{item['facet']}：{syn}（置信{conf:.0%}）")
        tensions = self.data.get("open_tensions", [])
        if tensions:
            lines.append("待澄清张力：" + "；".join(tensions[:5]))
        if not lines:
            return ""
        return "【用户画像·辩证累积】（跨会话了解用户，用于个性化回应）\n" + "\n".join(lines)

    def get_profile(self) -> str:
        """兼容旧接口：返回可读文本（用于 UI 展示）。"""
        return self.format_for_prompt()

    # ---- 兼容旧版 update() 调用（nudge 调用） ----
    def update(self, recent_history: str, llm_client=None) -> bool:
        changed = self.observe(recent_history, llm_client=llm_client)
        if changed and llm_client is not None:
            try:
                self.reconcile(llm_client=llm_client)
            except Exception as e:
                logger.warning(f"张力综合失败：{e}")
        return changed

    def to_dict(self) -> dict:
        return self.data
