"""Agent 主类 —— 核心对话循环（阶段一：增加流式工具调用）"""

import json
import logging
from typing import Optional, Generator

from core.llm import LLMClient
from core.proactive import ProactiveEngine
from core.skills_evolution import SKILL_EVOLVER as skill_evolver_singleton
from core.user_model import UserModeler
from core.nudge import SelfNudge
from core.mcp_client import MCPManager
from core.context_files import ContextFileLoader, SubdirectoryHintTracker
from core.rl_loop import RLLoop
from core.permissions import PermissionManager, ALLOW, DENY, ASK
from core.context_compressor import compress_if_needed
from core.subagents import _set_current_agent  # noqa: 子智能体编排(P1)
from core.scheduler import SCHEDULER  # noqa: 定时调度单例(P1)
from core.hooks import HookManager  # noqa: 事件钩子(P1b)
from core.commands import Commands  # noqa: 斜杠命令(P1b)
import core.commands  # noqa: 注册 slash_command 工具
import tools.browser  # noqa: 注册 browser 工具(P1b)
from memory.manager import MemoryManager
from skills.actor import Actor
from tools.registry import registry as tool_registry
import tools.builtin  # noqa: 注册内置工具
import tools.shell  # noqa: 注册 run_command 执行工具(P0)
import core.subagents  # noqa: 注册 delegate_task / moa 工具(P1)
import core.scheduler  # noqa: 注册 cronjob 工具(P1)

logger = logging.getLogger(__name__)

# 供 rl_distill 工具取用当前 agent 的 LLM 客户端（模块级单例句柄）
_CURRENT_LLM = None


def _get_agent_llm():
    return _CURRENT_LLM


class Agent:
    """AI Agent 主类"""

    def __init__(self, backend: str | None = None, provider: str | None = None,
                 model: str | None = None):
        self.backend = backend
        self.llm = LLMClient(backend=backend, provider=provider, model=model)
        self.memory = MemoryManager()
        self.actor = Actor()
        self.proactive = ProactiveEngine(self.memory)
        # 自进化组件（参照 Hermes 闭环）
        self.skill_evolver = skill_evolver_singleton
        self.user_modeler = UserModeler()
        self.nudge = SelfNudge(self.memory)
        # MCP：连接配置中的外部服务器并注册其工具
        self.mcp = MCPManager()
        try:
            self.mcp.connect_all(timeout=10)
        except Exception as e:
            logger.warning(f"MCP 连接初始化跳过：{e}")
        # 上下文文件优先级链（对齐 Hermes context-files）：SOUL 身份 + 项目上下文 + 渐进发现
        self.context_loader = ContextFileLoader()
        self.subdir_tracker = SubdirectoryHintTracker()
        # RL 训练回路（对齐 Hermes/Atropos 的 agent 侧部分）：轨迹捕获 + 奖励 + 蒸馏 + 导出
        self.rl = RLLoop()
        # P0：工具权限审批系统（对齐 Hermes tool_guardrails）
        self.permissions = PermissionManager()
        self._last_compressed = False
        global _CURRENT_LLM
        _CURRENT_LLM = self.llm
        # P1：把当前 agent 句柄交给子智能体模块（供 delegate_task / moa 工具取用）
        _set_current_agent(self)
        # P1：定时调度器——挂载 agent + 启动后台 tick 线程（try/except 防止拖垮主流程）
        self.scheduler = SCHEDULER
        try:
            SCHEDULER.set_agent(self)
            SCHEDULER.start()
        except Exception as e:
            logger.warning(f"调度器启动跳过：{e}")
        # P1b：事件钩子（非阻塞，异常被忽略）
        self.hooks = HookManager()
        try:
            self.hooks.session_started()
        except Exception as e:
            logger.warning(f"钩子 session_start 跳过：{e}")
        # P1b：斜杠命令
        self.commands = Commands()
        self._turns_since_crystallize = 0
        self.last_tool_calls = 0
        self.system_prompt = (
            "你是一个智能友好的 AI 伴侣，名叫小伴。\n"
            "用中文回答，语气亲切自然。\n\n"
            "【工具使用】\n"
            "你可以调用以下工具来获取实时信息，帮助用户更好地解决问题：\n"
            "- 天气查询、网络搜索、新闻获取、计算器、时间查询\n"
            "当用户的问题需要实时数据时，主动使用工具，不要编造信息。\n"
            "工具调用失败时，诚实告知用户并尝试其他方式帮助用户。\n\n"
            "【记忆】\n"
            "如果用户提到自己的名字、职业、所在地，记住这些信息。\n\n"
            "【切换人物模式】\n"
            "用户说出以下关键词时，激活对应的人物思维模式：\n"
            "- 切换到某人的视角\n"
            "- 用XX的语气回答\n"
            "- 女娲，帮我蒸馏XX\n"
            "- 我想用XX的思维框架\n"
        )

    def _build_messages(
        self, session_id: str, user_message: str, images: list[str] | None = None
    ) -> list[dict]:
        """构建完整的消息列表。

        images: 图片的 base64 data URL 或 http(s) URL 列表（仅当主模型支持视觉时传入）。
        """
        role_override = self.actor.get_system_prompt_override()

        if role_override:
            sys_prompt = role_override
        else:
            # 普通聊天模式：注入含工具说明的系统提示 + 语义召回的用户记忆
            memory_prompt = self.memory.get_memory_prompt(session_id, user_message)
            sys_prompt = f"{self.system_prompt}\n\n{memory_prompt}"

        # 注入「已掌握的技能」与「用户画像」（自进化：跨会话积累，越用越强）
        skills_block = self.skill_evolver.retrieve_relevant(user_message)
        profile_block = self.user_modeler.format_for_prompt()
        extra = "\n\n".join(b for b in (skills_block, profile_block) if b)
        if extra:
            sys_prompt = f"{sys_prompt}\n\n{extra}"

        # 注入上下文文件（SOUL 身份 + 项目上下文，按 Hermes 优先级链）
        ctx_block = self.context_loader.load_startup()
        if ctx_block:
            sys_prompt = f"{sys_prompt}\n\n{ctx_block}"

        # 注入事件钩子上下文（session_start + user_message + pre_llm_call 累积）
        hook_ctx = self.hooks.before_llm(session_id, user_message)
        if hook_ctx:
            sys_prompt = f"{sys_prompt}\n\n[Hook 注入]\n{hook_ctx}"

        messages = [{"role": "system", "content": sys_prompt}]

        history = self.memory.get_history(session_id, limit=20)
        messages.extend(history)

        # 多模态用户消息：支持视觉的模型直接收图片内容块
        if images:
            content: list[dict] = [{"type": "text", "text": user_message}]
            for img in images:
                content.append({"type": "image_url", "image_url": {"url": img}})
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": "user", "content": user_message})
        return messages

    def _execute_tool_calls(self, tool_calls: list) -> list[dict]:
        """执行一批工具调用，返回 tool 消息列表"""
        results = []
        for tc in tool_calls:
            name = tc.function.name
            args = {}
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                args = {}
            result = self._run_tool_guarded(name, args)
            # 渐进式上下文发现：工具触及的文件/目录若含上下文文件，注入其后的结果
            result = self._observe_context(name, args, result)
            results.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": str(result),
            })
        return results

    def _run_tool_guarded(self, name: str, args: dict) -> str:
        """带权限审批的工具执行入口（P0：对齐 Hermes tool_guardrails）。"""
        tool = tool_registry.get(name)
        if tool is None:
            return f"工具 {name} 未找到"
        decision, reason = self.permissions.check(name, args)
        if decision == DENY:
            logger.warning(f"权限拒绝工具 {name}: {reason}")
            return f"⛔ 权限拒绝：{reason}"
        if decision == ASK:
            logger.info(f"工具 {name} 待授权: {reason}")
            return (
                f"🔐 需要授权：{reason}\n"
                f"（当前为 ask 模式：可在 UI 点击「授权并执行」，"
                f"或设置 TOOL_PERMISSION_MODE=allow 放行 / deny 拒绝）"
            )
        # 事件钩子：pre_tool_call 可否决（对齐 Hermes block 语义）
        try:
            blocked, block_reason = self.hooks.pre_tool_call(name, args)
            if blocked:
                logger.info(f"钩子否决工具 {name}: {block_reason}")
                return f"⛔ 被钩子(pre_tool_call)否决：{block_reason}"
        except Exception as e:
            logger.warning(f"pre_tool_call 钩子异常被忽略：{e}")
        try:
            result = str(tool.run(**(args or {})))
            self.hooks.post_tool_call(name, args, result)
            return result
        except json.JSONDecodeError:
            return f"参数解析错误：{args}"
        except Exception as e:
            logger.error(f"工具执行失败 {name}: {e}")
            self.hooks.on_error(e)
            return f"工具执行错误：{e}"

    # ---- 上下文文件渐进发现（对齐 Hermes SubdirectoryHintTracker） ----
    @staticmethod
    def _extract_paths(args: dict) -> list[str]:
        """从工具参数里抽取看起来像路径的字符串（用于触发上下文发现）。"""
        paths: list[str] = []
        if not isinstance(args, dict):
            return paths
        path_keys = {"path", "workdir", "dir", "directory", "file", "root", "cwd"}
        for k, v in args.items():
            if isinstance(v, str) and (k.lower() in path_keys or "/" in v or "\\" in v):
                paths.append(v)
        return paths

    def _observe_context(self, tool_name: str, args: dict, result) -> str:
        """工具执行后触发渐进式上下文发现，把发现的上下文追加到工具结果之后。"""
        try:
            paths = self._extract_paths(args)
            if paths:
                extra = self.subdir_tracker.observe(paths)
                if extra:
                    return f"{result}\n\n{extra}"
        except Exception:
            pass
        return str(result)

    # ---- RL 轨迹捕获（对齐 Hermes/Atropos 的 agent 侧回路） ----
    def _record_trajectory(self, session_id, prompt, answer, tool_calls, messages):
        try:
            self.rl.capture(session_id, prompt, messages, tool_calls, answer)
        except Exception as e:
            logger.warning(f"RL 轨迹捕获失败：{e}")

    def draft_plan(self, session_id: str, user_message: str) -> str:
        """计划模式（P0）：只产出执行方案，不调用任何工具（对齐 Hermes 计划模式）。"""
        messages = self._build_messages(session_id, user_message)
        # 在系统提示末尾追加规划指令
        sys_content = messages[0]["content"] + (
            "\n\n【计划模式】你当前处于计划模式。请只输出执行方案，不要调用任何工具，"
            "不要执行操作。方案应分步骤、清晰、可复核，并在末尾说明"
            "「确认后我将按此执行」。"
        )
        messages[0] = {"role": "system", "content": sys_content}
        try:
            resp = self.llm.chat(messages=messages, temperature=0.3)
            return resp.choices[0].message.content or ""
        except Exception as e:
            return f"计划生成失败：{e}"

    def chat(self, session_id: str, user_message: str, images: list[str] | None = None) -> str:
        """一次对话交互（非流式，含工具调用）"""
        self.memory.process_message(session_id, "user", user_message)
        messages = self._build_messages(session_id, user_message, images=images)
        # 上下文压缩（P0）
        messages, self._last_compressed = compress_if_needed(messages, self.llm)

        response = self.llm.chat(
            messages=messages,
            temperature=0.7,
            tools=tool_registry.to_openai_tools(),
        )
        reply = response.choices[0].message.content or ""

        if response.choices[0].message.tool_calls:
            messages.append(response.choices[0].message)
            tool_completions = self._execute_tool_calls(
                response.choices[0].message.tool_calls
            )
            messages.extend(tool_completions)

            final = self.llm.chat(messages=messages)
            reply = final.choices[0].message.content or ""

        self.memory.process_message(session_id, "assistant", reply)
        # RL 轨迹捕获
        tool_calls_summary = []
        if response.choices[0].message.tool_calls:
            for tc in response.choices[0].message.tool_calls:
                try:
                    tool_calls_summary.append({
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    })
                except Exception:
                    pass
        self._record_trajectory(session_id, user_message, reply, tool_calls_summary, messages)
        self.hooks.assistant_message(reply)
        return reply

    def chat_stream(
        self, session_id: str, user_message: str,
        images: list[str] | None = None, files: list[dict] | None = None,
        plan_mode: bool = False,
    ) -> Generator[str, None, None]:
        """流式对话（阶段一：支持工具调用）

        当 LLM 返回 tool_calls 时：
        1. 收集所有 tool_call 增量
        2. 执行工具（经权限审批门控）
        3. 将结果注入上下文
        4. 非流式二次调用生成最终回复
        5. yield 最终回复

        images: 图片 data/URL 列表（有视觉能力的模型直接看像素；否则走辅助视觉模型转文字）
        files:  [{name, content}] 本地附件，作为文本上下文注入
        plan_mode: True 时只产出执行方案、不调用任何工具（对齐 Hermes 计划模式）
        """
        images = list(images or [])
        files = list(files or [])

        # 视觉回退（Hermes 的 vision_analyze 思路）：主模型无视觉能力时，
        # 用辅助视觉模型把图片描述成文字，注入对话，避免坏的多模态负载。
        if images and not self.llm.supports_vision:
            desc = self.llm.describe_image(images)
            if desc:
                user_message = f"{user_message}\n\n[用户附带的图片内容描述：{desc}]"
            else:
                user_message = (
                    f"{user_message}\n\n[注：当前模型不支持图片识别，且未配置辅助视觉模型，已忽略图片]"
                )
            images = []

        # 文件附件作为文本上下文注入
        if files:
            file_ctx = "\n\n".join(
                f"[附件文件：{f['name']}]\n{f['content']}" for f in files
            )
            user_message = f"{user_message}\n\n{file_ctx}"

        self.memory.process_message(session_id, "user", user_message)

        # 计划模式（P0）：仅规划，不执行任何工具
        if plan_mode:
            yield "\n\n📋 **计划模式** —— 仅输出方案，不执行操作：\n\n"
            plan = self.draft_plan(session_id, user_message)
            yield plan
            self.memory.process_message(session_id, "assistant", plan)
            return

        messages = self._build_messages(session_id, user_message, images=images)
        # 上下文压缩（P0）：超阈值时把旧轮次摘要化，非破坏式
        messages, self._last_compressed = compress_if_needed(messages, self.llm)
        self.last_tool_calls = 0  # 本轮工具调用计数（供轮后结晶门控使用）

        stream = self.llm.chat_stream(
            messages=messages,
            temperature=0.7,
            tools=tool_registry.to_openai_tools(),
        )

        full_reply = ""
        tool_call_accumulator = {}  # 累积流式返回的 tool_call 增量
        tool_call_dicts: list = []  # 本轮工具调用摘要（供 RL 轨迹捕获）

        for chunk in stream:
            delta = chunk.choices[0].delta

            # 处理内容输出
            if delta.content:
                full_reply += delta.content
                yield delta.content

            # 推理/思考过程（P0）：部分模型在 delta 中回传 reasoning_content
            rc = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
            if rc:
                yield f"🧠 {rc}"

            # 累积 tool_calls 增量
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_call_accumulator:
                        tool_call_accumulator[idx] = {
                            "id": tc_delta.id or "",
                            "function_name": tc_delta.function.name or "" if tc_delta.function else "",
                            "arguments": "",
                        }
                    acc = tool_call_accumulator[idx]
                    if tc_delta.id:
                        acc["id"] = tc_delta.id
                    if tc_delta.function and tc_delta.function.name:
                        acc["function_name"] = tc_delta.function.name
                    if tc_delta.function and tc_delta.function.arguments:
                        acc["arguments"] += tc_delta.function.arguments

        # 如果有工具调用，执行并生成最终回复
        if tool_call_accumulator:
            # 先向 UI 展示工具调用状态
            for idx in sorted(tool_call_accumulator.keys()):
                acc = tool_call_accumulator[idx]
                yield f"\n\n🔧 正在调用工具 `{acc['function_name']}`...\n"

            # 构造 assistant 消息（含 tool_calls），dict 格式兼容 OpenAI SDK
            tool_call_dicts = []
            for idx in sorted(tool_call_accumulator.keys()):
                acc = tool_call_accumulator[idx]
                tool_call_dicts.append({
                    "id": acc["id"],
                    "type": "function",
                    "function": {
                        "name": acc["function_name"],
                        "arguments": acc["arguments"],
                    },
                })
            self.last_tool_calls = len(tool_call_dicts)  # 记录本轮工具调用数

            # 注入 assistant 消息
            messages.append({
                "role": "assistant",
                "content": full_reply or None,
                "tool_calls": tool_call_dicts,
            })

            # 执行工具并展示结果（经权限审批门控）
            for tc in tool_call_dicts:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    args = {}
                result = self._run_tool_guarded(name, args)
                # 渐进式上下文发现：工具触及的文件/目录若含上下文文件，注入其后的结果
                result = self._observe_context(name, args, result)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })
                preview = result[:300] + ("..." if len(result) > 300 else "")
                yield f"✅ `{name}` 返回：{preview}\n"

            # 二次调用（非流式，不传 tools 避免再次触发工具循环）
            final = self.llm.chat(messages=messages)
            reply = final.choices[0].message.content or ""
            yield "\n\n" + reply
            full_reply = reply

        self.memory.process_message(session_id, "assistant", full_reply or "(tool calls only)")
        # RL 轨迹捕获（agent 侧回路的起点）
        self._record_trajectory(session_id, user_message, full_reply, tool_call_dicts, messages)
        self.hooks.assistant_message(full_reply or "")

    # ---- 主动对话（P2-2）----
    def greeting(self) -> str:
        """首次进入时的欢迎语。"""
        return self.proactive.greeting()

    def proactive_ping(self, session_id: str) -> str:
        """主动开启一个话题（基于时间段 + 记忆）。"""
        return self.proactive.suggest_ping(session_id, llm_client=self.llm)

    # ---- 自进化（参照 Hermes 闭环） ----
    def evolve_after_turn(
        self, session_id: str, user_message: str, assistant_reply: str
    ) -> dict | None:
        """一轮对话结束后，尝试把多步任务结晶为可复用技能。

        由 UI 在 st.write_stream 返回后调用。带周期门控：
        - 必须是多步任务（>=2 次工具调用）；
        - 每 3 轮多步任务才结晶一次，避免频繁打扰与浪费 token；
        - 去重由 SkillEvolver 内部完成。
        返回新技能 dict（未触发则返回 None）。
        """
        self._turns_since_crystallize += 1
        if self.last_tool_calls < 2:
            return None
        if self._turns_since_crystallize < 3:
            return None
        self._turns_since_crystallize = 0
        return self.skill_evolver.maybe_crystallize(
            user_message=user_message,
            assistant_reply=assistant_reply,
            tool_calls=self.last_tool_calls,
            llm_client=self.llm,
            session_id=session_id,
        )

    def run_nudge(self, session_id: str) -> str:
        """主动做一次自我整理（更新画像 + 蒸馏旧记忆），返回摘要文本。"""
        return self.nudge.run(session_id, llm_client=self.llm)

    # ---- P1：子智能体编排（对齐 Hermes 委派与并行 / MoA） ----
    def delegate(self, prompt: str, role: str = None, focus: str = None) -> str:
        """委派一个子任务给隔离的子智能体，返回其结果。"""
        from core.subagents import run_subagent
        return run_subagent(self, prompt, role=role, focus=focus, depth=1)

    def moa(self, prompt: str, num_references: int = None,
             advisor_specs: str = None, aggregator_spec: str = None) -> str:
        """Mixture-of-Agents：多顾问并行分析 + 聚合者综合，返回最终答案。

        advisor_specs：真实多模型池（"provider:model" 逗号分隔），指定后每个顾问用不同模型。
        aggregator_spec：聚合者模型（"provider:model"），指定则单轮综合；留空用主模型+工具。
        """
        from core.subagents import moa as _moa
        return _moa(self, prompt, num_references=num_references, depth=1,
                    advisor_specs=advisor_specs, aggregator_spec=aggregator_spec)