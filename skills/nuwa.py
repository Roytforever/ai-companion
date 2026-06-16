"""女娲造人 — 自动蒸馏系统

从人物名称/主题出发，通过多源调研→框架提炼→Skill生成，创建可运行的人物视角。
简化版：适用于项目集成，核心流程保留但去掉复杂Agent并行。
"""

from __future__ import annotations

from skills.registry import PerspectiveRegistry
from skills.loader import load_all_perspectives


# 需求维度映射表 — 从用户模糊需求反推适合的思维框架
DEMAND_DIMENSIONS = {
    "决策": {"tags": ["决策", "思维模型", "商业"], "description": "多元思维模型、逆向思考、概率思维"},
    "表达": {"tags": ["写作", "表达", "沟通"], "description": "费曼式简化、故事化思维、类比能力"},
    "创业": {"tags": ["创业", "商业", "科技"], "description": "第一性原理、杠杆思维、产品克制"},
    "投资": {"tags": ["投资", "风险"], "description": "复利思维、风险控制、逆向投资"},
    "教育": {"tags": ["教育", "学习"], "description": "费曼学习法、知行合一、终身学习"},
    "写作": {"tags": ["写作", "创意"], "description": "故事化表达、黄金圈法则、克制美学"},
    "领导力": {"tags": ["领导力", "管理"], "description": "成事心法、反脆弱组织、文化驱动"},
    "产品": {"tags": ["产品", "设计", "科技"], "description": "极简主义、用户心理模型、约束即创意"},
    "人生": {"tags": ["人生", "哲学", "幸福"], "description": "长期主义、杠杆选择、复利思维"},
    "风险": {"tags": ["风险", "概率"], "description": "反脆弱、凸性策略、尾部风险管理"},
}


class Nuwa:
    """女娲 — 人物思维框架蒸馏引擎"""

    def __init__(self):
        self.registry = load_all_perspectives()

    def diagnose(self, demand: str) -> list[dict]:
        """从用户模糊需求诊断并推荐合适的人物视角"""
        demand_lower = demand.lower()

        # 找出匹配的需求维度
        matched = []
        for keyword, info in DEMAND_DIMENSIONS.items():
            if keyword in demand_lower:
                matched.append((keyword, info))

        if not matched:
            # 尝试更宽松的匹配：看任何中文关键词
            for keyword, info in DEMAND_DIMENSIONS.items():
                if any(char in demand for char in keyword):
                    matched.append((keyword, info))

        # 从registry中找匹配tag的视角
        candidates = []
        seen = set()
        for keyword, info in matched:
            tag = info["tags"][0]  # 取第一个tag
            for p in self.registry.list_all():
                if p.id in seen:
                    continue
                if any(t == tag or tag in t for t in p.tags):
                    candidates.append({
                        "id": p.id,
                        "name": p.name,
                        "name_en": p.name_en,
                        "description": p.description,
                        "reason": f"擅长{info['description']}相关的问题",
                    })
                    seen.add(p.id)

        # 如果没有匹配，返回前几个最通用的
        if not candidates:
            for p in self.registry.list_all()[:5]:
                candidates.append({
                    "id": p.id,
                    "name": p.name,
                    "name_en": p.name_en,
                    "description": p.description,
                    "reason": p.tags[0] if p.tags else "通用视角",
                })

        return candidates[:5]

    def distill(self, person_name: str, user_provided_materials: str = "") -> str:
        """蒸馏一个人物的思维框架，返回SKILL内容

        这是简化版。完整版需要多Agent并行调研、框架提炼、质量验证。
        当前版本生成一个基础的SKILL.md框架供用户使用。
        """
        # 检查是否已存在
        existing = self.registry.search(person_name)
        if existing:
            found = existing[0]
            return f"【{found.name}】已存在于系统中。\n\n{found.name}（{found.name_en}）\n{found.description}\n\n可以直接使用，无需重新蒸馏。"

        # 生成新的Skill框架
        skill_content = self._generate_skill_skeleton(person_name, user_provided_materials)

        # 注册到registry
        pid = person_name.lower().replace(" ", "-").replace("·", "-")
        self.registry.register(Perspective(
            id=pid,
            name=person_name,
            name_en=person_name,
            description=f"基于用户提供的素材蒸馏的 {person_name} 思维框架",
            tags=["distilled"],
            trigger_words=[person_name],
            skill_content=skill_content,
            source="distilled",
        ))

        return skill_content

    def _generate_skill_skeleton(self, person_name: str, materials: str) -> str:
        """生成基础的SKILL.md框架"""
        return f"""---
name: {person_name.lower().replace(" ", "-")}-perspective
description: |
  基于用户提供的素材蒸馏的{person_name}思维框架。
  用途：作为思维顾问，用{person_name}的视角分析问题、审视决策、提供反馈。
---

# {person_name} · 思维操作系统

## 角色扮演规则

**此Skill激活后，直接以{person_name}的身份回应。**
- 用「我」而非「{person_name}会认为...」
- 直接用此人的语气、节奏、词汇回答问题
- 遇到不确定的问题，用此人会有的方式回应

**退出角色**：用户说「退出」「切回正常」「不用扮演了」时恢复正常模式

## 身份卡

**我是谁**：我是{person_name}。

（基于用户提供的素材生成）

## 心智模型

（需要深度调研后提炼）

## 决策启发式

（需要深度调研后提取）

## 表达DNA

（需要素材分析后总结）

## 诚实边界

- 本Skill基于用户提供的有限素材生成，完整性有限
- 不能预测面对全新问题的反应
- 不能替代此人的创造力和直觉
- 信息截止到素材提供时间

## 调研来源
- 用户提供素材
"""


# 重复导入问题修复 — 确保本文件可以独立使用
from skills.registry import Perspective