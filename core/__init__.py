"""大模型调用封装"""

from typing import Optional
from openai import OpenAI, Stream
from openai.types.chat import ChatCompletion, ChatCompletionChunk

from config import settings


class LLMClient:
    """封装 OpenAI 兼容接口的大模型调用"""

    def __init__(self):
        self.client = OpenAI(
            api_key=settings.LLM_API_KEY,
            base_url=settings.LLM_BASE_URL,
        )
        self.model = settings.LLM_MODEL
        self._validate_config()

    def _validate_config(self):
        if not settings.LLM_API_KEY:
            raise ValueError(
                "LLM_API_KEY 未设置！请在 .env 文件中配置你的 API Key。\n"
                "支持 DeepSeek / OpenAI / 通义千问等兼容接口。"
            )

    def chat(
        self,
        messages: list[dict],
        stream: bool = False,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> ChatCompletion | Stream[ChatCompletionChunk]:
        """发送聊天请求"""
        kwargs = dict(
            model=self.model,
            messages=messages,
            stream=stream,
            temperature=temperature,
        )
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        return self.client.chat.completions.create(**kwargs)

    def chat_stream(self, messages: list[dict], **kwargs):
        """流式聊天"""
        return self.chat(messages, stream=True, **kwargs)