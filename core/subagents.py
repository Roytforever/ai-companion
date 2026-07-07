"""子智能体编排（对齐 Hermes「委派与并行 / Mixture-of-Agents」）。

核心思想：
- SubAgent：完全隔离的子会话（无主会话历史、不写主记忆），可跑带工具的循环，
  用于把复杂任务拆给「分身」并行 / 串行处理。
- MoA（混合智能体，Mixture of Agents）：多个「参考顾问」(reference advisor) 并行产出
  多角度分析，再由「聚合者」(aggregator) 综合成最终答案——复用主模型的完整工具能力。
- 递归防护：子智能体 / cron 会话内禁用 moa / delegate_task / subagent_run / cronjob，
  且递归深度受 SUBAGENT_MAX_DEPTH 限制，防止 token 爆炸与无限自我委派。

本模块不导入 agent（避免循环依赖）；运行期由 core.agent 把当前 agent 句柄写入
_CURRENT_AGENT，工具函数据此取用同一套 llm 与工具注册表。
"""
import json
import logging
from concurrent.futures import ThreadPoolExecutor

from config import settings
from tools.registry import registry as tool_registry

logger = logging.getLogger(__name__)

# 递归防护：这些工具在子智能体 / cron 会话内被禁用
_RECURSION_BLOCKED = {"moa", "delegate_task", "subagent_run", "cronjob"}

# 当前 agent 句柄（由 core.agent 初始化时写入）
_CURRENT_AGENT = None


def _set_current_agent(agent):
    global _CURRENT_AGENT
    _CURRENT_AGENT = agent


def _msg_to_dict(msg):
    """把 OpenAI SDK 的 assistant message 对象转成可入历史链的 dict。"""
    d = {"role": "assistant", "content": msg.content or ""}
    tcs = getattr(msg, "tool_calls", None)
    if tcs:
        d["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in tcs
        ]
    return d


class SubAgent:
    """一个隔离的子会话：自带消息历史，可跑工具循环，不污染主会话记忆。"""

    def __init__(self, agent, role=None, focus=None, max_iter=None, depth=0,
                 disabled_tools=None):
        self.agent = agent
        self.role = role
        self.focus = focus
        self.max_iter = max_iter or settings.SUBAGENT_MAX_ITER
        self.depth = depth
        self.disabled = set(_RECURSION_BLOCKED) | set(disabled_tools or [])
        self.messages = []

    def _system_prompt(self):
        parts = [self.agent.system_prompt]
        if self.role:
            parts.append(f"\n\n【子任务角色】你正作为「{self.role}」独立分析以下子任务。")
        if self.focus:
            parts.append(f"\n【聚焦方向】请重点关注：{self.focus}")
        parts.append(
            "\n\n你运行在一个隔离的子智能体会话中，看不到主对话历史。"
            "请基于给定任务自洽地完成，并产出清晰、可直接被主智能体复用的结论。"
        )
        return "".join(parts)

    def _openai_tools(self):
        tools = tool_registry.to_openai_tools()
        if self.disabled:
            tools = [t for t in tools if t["function"]["name"] not in self.disabled]
        return tools

    def run(self, prompt: str) -> str:
        try:
            return self._run_loop(prompt)
        except Exception as e:
            logger.error(f"子智能体执行失败：{e}")
            return f"（子智能体执行出错：{e}）"

    def _run_loop(self, prompt: str) -> str:
        sys_msg = {"role": "system", "content": self._system_prompt()}
        # 子智能体同样可读取已掌握技能（只读，不结晶）
        skills_block = self.agent.skill_evolver.retrieve_relevant(prompt)
        if skills_block:
            sys_msg["content"] += "\n\n" + skills_block
        self.messages = [sys_msg, {"role": "user", "content": prompt}]

        for _ in range(self.max_iter):
            resp = self.agent.llm.chat(
                messages=self.messages,
                temperature=0.7,
                tools=self._openai_tools(),
            )
            msg = resp.choices[0].message
            if not msg.tool_calls:
                return msg.content or ""
            self.messages.append(_msg_to_dict(msg))
            for tc in msg.tool_calls:
                name = tc.function.name
                if name in self.disabled:
                    result = f"⛔ 子智能体 / 定时会话中已禁用工具 `{name}`"
                else:
                    try:
                        args = json.loads(tc.function.arguments)
                    except Exception:
                        args = {}
                    result = self.agent._run_tool_guarded(name, args)
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": str(result),
                })

        # 超过迭代上限：再调一次让模型做总结
        final = self.agent.llm.chat(messages=self.messages)
        return final.choices[0].message.content or ""


def run_subagent(agent, prompt, role=None, focus=None, depth=0,
                 max_iter=None, disabled_tools=None) -> str:
    """运行一个隔离子智能体（供委派 / cron 复用）。"""
    if depth >= settings.SUBAGENT_MAX_DEPTH:
        return "（已达到子智能体最大递归深度，停止继续委派）"
    sa = SubAgent(
        agent, role=role, focus=focus, max_iter=max_iter,
        depth=depth, disabled_tools=disabled_tools,
    )
    return sa.run(prompt)


# ---------------- Mixture of Agents (MoA) ----------------

_PERSPECTIVE_FALLBACK = [
    "技术可行性与实现路径",
    "潜在风险与权衡",
    "用户价值与体验",
    "长期影响与可扩展性",
    "成本与资源投入",
]


def _gen_perspectives(agent, prompt: str, n: int) -> list[str]:
    """让模型把任务拆成 N 个互补视角；失败则用通用模板兜底。"""
    try:
        r = agent.llm.chat(messages=[
            {"role": "system", "content": "你是一个任务分解助手。针对用户问题，"
             "列出若干个互补的分析视角，每行一个，简短清晰，不要编号前缀。"},
            {"role": "user", "content": f"问题：{prompt}\n请列出 {n} 个互补的分析视角："},
        ], temperature=0.8)
        lines = [
            ln.strip("0123456789.、-★• \t") for ln in
            (r.choices[0].message.content or "").splitlines()
            if ln.strip()
        ]
        lines = [ln for ln in lines if ln][:n]
        if len(lines) >= 1:
            return lines
    except Exception as e:
        logger.warning(f"MoA 视角生成失败，使用兜底模板：{e}")
    return _PERSPECTIVE_FALLBACK[: max(n, 1)]


def _build_aggregator_prompt(prompt: str, advices: list[str]) -> str:
    parts = [f"# 原始问题\n{prompt}\n\n# 多位独立顾问的分析\n"]
    for i, a in enumerate(advices, 1):
        parts.append(f"## 顾问 {i} 的分析\n{a}\n")
    parts.append(
        "# 你的任务\n综合以上多位顾问的分析，给出一个综合、准确、可直接使用的最终答案。"
        "融合各方优点，如有冲突请权衡取舍并说明依据。"
    )
    return "\n".join(parts)


def _parse_advisor_specs(specs_str):
    """把 "provider:model" 字符串解析为 [(provider, model), ...]。

    - "deepseek:deepseek-chat" -> ("deepseek", "deepseek-chat")
    - "qwen"                  -> ("qwen", None)          （已知 provider，用其默认模型）
    - "gpt-4o-mini"           -> (None, "gpt-4o-mini")   （未知，用主 provider + 该 model）
    - "provider:"             -> ("provider", None)
    """
    from core.providers import list_providers
    known = {p.name for p in list_providers()}
    out = []
    for raw in (specs_str or "").split(","):
        s = raw.strip()
        if not s:
            continue
        if ":" in s:
            prov, model = s.split(":", 1)
            prov, model = prov.strip(), model.strip()
        else:
            if s in known:
                prov, model = s, ""
            else:
                prov, model = "", s
        out.append((prov or None, model or None))
    return out


def _make_advisor_llm(provider, model, fallback_llm):
    """按 (provider, model) 构造一个独立的 LLMClient；失败则回退主模型 client。"""
    if provider is None and model is None:
        return fallback_llm
    from core import LLMClient
    try:
        return LLMClient(provider=provider, model=model)
    except Exception as e:
        logger.warning(f"MoA 顾问模型构造失败({provider}:{model})，回退主模型：{e}")
        return fallback_llm


def moa(agent, prompt: str, num_references=None, depth=0,
        advisor_specs=None, aggregator_spec=None) -> str:
    """Mixture of Agents：并行多个参考顾问 + 聚合者综合（对齐 Hermes MoA）。

    两种模式（由 advisor_specs 决定）：
    - 真实多模型池：advisor_specs 非空时，每个顾问用独立 LLMClient
      （provider 注册表解析不同模型），单轮并行分析，模型差异本身提供多样性。
    - 同模型多视角（旧行为）：advisor_specs 为空时，所有顾问复用主模型，
      仅通过 prompt 视角（_gen_perspectives）产生差异，且保留工具能力。
    聚合者：aggregator_spec 指定模型则单轮综合；否则用主模型 + 完整工具能力。
    """
    if depth >= settings.SUBAGENT_MAX_DEPTH:
        # 退化为普通回答，避免无谓递归
        return agent.chat(prompt)

    # 解析顾问模型池（显式参数优先，否则读配置）
    specs = _parse_advisor_specs(advisor_specs) if advisor_specs \
        else _parse_advisor_specs(settings.MOA_ADVISOR_MODELS)

    if specs:
        # ---------- 真实多模型池模式 ----------
        n = num_references or max(len(specs), 1)
        # 若 num_references 超过 specs 数量，循环补足后截断
        selected = (specs * (n // len(specs) + 1))[:n]

        def _run_advisor(spec):
            prov, model = spec
            llm = _make_advisor_llm(prov, model, agent.llm)
            try:
                r = llm.chat(messages=[
                    {"role": "system", "content":
                        "你是一位独立分析顾问。请针对用户问题，基于你模型自身的"
                        "知识、风格与擅长的角度，给出独立、深入、有见解的分析结论。"
                        "不要复述问题，直接给分析，控制在 300 字内。"},
                    {"role": "user", "content": f"问题：{prompt}"},
                ], temperature=0.7)
                return (r.choices[0].message.content or "").strip() or "（顾问未返回内容）"
            except Exception as e:
                logger.error(f"MoA 顾问({prov}:{model})调用失败：{e}")
                return f"（顾问 {prov}:{model} 调用失败：{e}）"

        with ThreadPoolExecutor(max_workers=max(len(selected), 1)) as ex:
            advices = list(ex.map(_run_advisor, selected))
    else:
        # ---------- 同模型多视角模式（旧行为，保留工具能力）----------
        n = num_references or settings.MOA_DEFAULT_REFERENCES
        perspectives = _gen_perspectives(agent, prompt, n)

        def _run_one(p):
            return run_subagent(
                agent, p, role="独立分析顾问",
                focus="给出该角度简明、独立的分析结论",
                depth=depth + 1, max_iter=2,
            )

        with ThreadPoolExecutor(max_workers=max(n, 1)) as ex:
            advices = list(ex.map(_run_one, perspectives))

    agg_prompt = _build_aggregator_prompt(prompt, advices)

    # 聚合者：指定模型则单轮综合；否则主模型 + 工具能力
    agg_prov, agg_model = (None, None)
    if aggregator_spec:
        _ag = _parse_advisor_specs(aggregator_spec)
        if _ag:
            agg_prov, agg_model = _ag[0]

    if agg_prov or agg_model:
        llm = _make_advisor_llm(agg_prov, agg_model, agent.llm)
        try:
            r = llm.chat(messages=[
                {"role": "system", "content":
                    "你是主聚合者，负责综合多位独立顾问的分析，给出最终答案。"},
                {"role": "user", "content": agg_prompt},
            ], temperature=0.5)
            return r.choices[0].message.content or ""
        except Exception as e:
            logger.error(f"MoA 聚合者调用失败：{e}")
            return agg_prompt  # 退化为原始聚合提示

    return run_subagent(
        agent, agg_prompt, role="主聚合者",
        focus="综合多方分析给出最终答案",
        depth=depth + 1, max_iter=3,
    )


# ---------------- 暴露给主 agent 的工具 ----------------

@tool_registry.register(
    name="delegate_task",
    description=(
        "把一个子任务委派给隔离的「子智能体」(sub-agent) 独立处理，返回其结果。"
        "适合把复杂任务拆分给分身并行或串行处理：role=扮演的角色(可选)，"
        "focus=该子任务应聚焦的方向(可选)。子智能体看不到主对话历史，prompt 需自包含。"
    ),
)
def delegate_task(prompt: str, role: str = "", focus: str = "") -> str:
    agent = _CURRENT_AGENT
    if agent is None:
        return "（agent 未就绪，无法委派）"
    return run_subagent(
        agent, prompt, role=role or None, focus=focus or None, depth=1,
    )


@tool_registry.register(
    name="moa",
    description=(
        "Mixture-of-Agents：把当前问题交给多个「参考顾问」并行分析，再由主聚合者综合成"
        "最终答案。适合需要多视角权衡的难题。返回综合后的最终答案文本。"
    ),
)
def moa_tool(prompt: str) -> str:
    agent = _CURRENT_AGENT
    if agent is None:
        return "（agent 未就绪，无法运行 MoA）"
    return moa(agent, prompt, depth=1)
