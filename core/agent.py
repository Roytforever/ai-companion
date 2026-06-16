"""Agent 主类 — 核心对话循环"""

from typing import Optional

from core.llm import LLMClient
from memory.manager import MemoryManager
from skills.actor import Actor
from tools.registry import registry as tool_registry
import tools.builtin  # noqa: 注册内置工具


class Agent:
    """AI Agent 主类"""

    def __init__(self):
        self.llm = LLMClient()
        self.memory = MemoryManager()
        self.actor = Actor()
        self.system_prompt = (
            "你是一个智能友好的 AI 伴侣，名叫小伴。\n"
            "用中文回答，语气亲切自然。\n"
            "你可以使用工具获取信息，但不要滥用工具。\n"
            "如果用户提到自己的名字、职业、所在地，记住这些信息。\n\n"
            "【切换人物模式】\n"
            "用户说出以下关键词时，激活对应的人物思维模式：\n"
            "- 切换到某人的视角\n"
            "- 用XX的语气回答\n"
            "- 女娲，帮我蒸馏XX\n"
            "- 我想用XX的思维框架\n"
        )

    def _build_messages(self, session_id: str, user_message: str) -> list[dict]:
        """构建完整的消息列表"""
        # 检查是否有角色扮演激活
        role_override = self.actor.get_system_prompt_override()

        if role_override:
            # 角色扮演模式下，用角色提示代替默认系统提示
            sys_prompt = role_override
        else:
            # 个性化系统提示
            sys_prompt = self.memory.get_system_prompt(session_id)

        messages = [{"role": "system", "content": sys_prompt}]

        # 加入历史消息
        history = self.memory.get_history(session_id, limit=20)
        messages.extend(history)

        # 加入当前用户消息
        messages.append({"role": "user", "content": user_message})
        return messages

    def chat(self, session_id: str, user_message: str) -> str:
        """一次对话交互（非流式）"""
        # 保存用户消息
        self.memory.process_message(session_id, "user", user_message)

        messages = self._build_messages(session_id, user_message)

        # 初次调用
        response = self.llm.chat(
            messages=messages,
            temperature=0.7,
        )
        reply = response.choices[0].message.content or ""

        # 检查是否有工具调用
        if response.choices[0].message.tool_calls:
            messages.append(response.choices[0].message)
            for tc in response.choices[0].message.tool_calls:
                tool = tool_registry.get(tc.function.name)
                if tool:
                    import json
                    try:
                        args = json.loads(tc.function.arguments)
                        result = tool.run(**args)
                    except Exception as e:
                        result = f"工具执行错误：{e}"
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": str(result),
                    })
            # 二次调用获取最终回复
            final = self.llm.chat(messages=messages)
            reply = final.choices[0].message.content or ""

        # 保存助手回复
        self.memory.process_message(session_id, "assistant", reply)
        return reply

    def chat_stream(self, session_id: str, user_message: str):
        """流式对话"""
        self.memory.process_message(session_id, "user", user_message)
        messages = self._build_messages(session_id, user_message)

        stream = self.llm.chat_stream(messages=messages)
        full_reply = ""
        for chunk in stream:
            if chunk.choices[0].delta.content:
                content = chunk.choices[0].delta.content
                full_reply += content
                yield content

        self.memory.process_message(session_id, "assistant", full_reply)