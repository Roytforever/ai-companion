"""大模型调用封装"""

import logging
from typing import Optional
from openai import OpenAI, Stream
from openai.types.chat import ChatCompletion, ChatCompletionChunk

from config import settings
from core.providers import resolve_provider, list_provider_models

logger = logging.getLogger(__name__)


class LLMClient:
    """封装 OpenAI 兼容接口的大模型调用（支持云端 / 本地 Ollama 两种后端）"""

    def __init__(self, backend: str | None = None, provider: str | None = None,
                 model: str | None = None, **overrides):
        # 1) 决定使用哪个 provider（优先级：显式 provider > backend > 默认配置）
        prov_name = None
        if provider:
            prov_name = provider
        elif backend == "local":
            prov_name = "ollama"
        elif backend == "cloud":
            prov_name = settings.LLM_PROVIDER or "deepseek"

        if prov_name:
            try:
                base_url, api_key, resolved_model, _supp_reason, _supp_vision = (
                    resolve_provider(prov_name, model)
                )
            except Exception as e:
                logger.warning(f"provider 解析失败({prov_name})，回退默认配置：{e}")
                prov_name = None

        if prov_name is None:
            # 旧路径：直接读 settings（兼容未配置 provider 的场景）
            params = settings.llm_client_kwargs
            if backend:
                if backend == "local":
                    params = {
                        "api_key": settings.LLM_API_KEY or "ollama",
                        "base_url": settings.LOCAL_MODEL_BASE_URL,
                        "model": settings.LOCAL_MODEL_NAME,
                        "timeout": settings.LLM_TIMEOUT,
                    }
                else:
                    params = {
                        "api_key": settings.LLM_API_KEY,
                        "base_url": settings.LLM_BASE_URL,
                        "model": settings.LLM_MODEL,
                        "timeout": settings.LLM_TIMEOUT,
                    }
            params.update(overrides)
            self.backend = backend or settings.LLM_BACKEND
            self.provider = self.backend
            self.model = params.get("model") or settings.LLM_MODEL
            api_key = params.get("api_key") or "ollama"
            base_url = params.get("base_url") or settings.LLM_BASE_URL
            timeout = params.get("timeout", settings.LLM_TIMEOUT)
        else:
            self.backend = backend
            self.provider = prov_name
            self.model = resolved_model
            timeout = settings.LLM_TIMEOUT

        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
        )
        self._validate_config()

        # 视觉能力：SUPPORTS_VISION 强制，否则按当前模型名启发式推断
        v = (settings.SUPPORTS_VISION or "").strip().lower()
        if v in ("true", "1", "yes", "y"):
            self.supports_vision = True
        elif v in ("false", "0", "no", "n"):
            self.supports_vision = False
        else:
            self.supports_vision = settings.model_supports_vision(self.model)

    def _validate_config(self):
        if self.backend == "cloud" and not settings.LLM_API_KEY:
            raise ValueError(
                "LLM_API_KEY 未设置！请在 .env 文件中配置你的 API Key。\n"
                "支持 DeepSeek / OpenAI / 通义千问等兼容接口。\n"
                "若想使用本地模型，请将 LLM_BACKEND 设为 local。"
            )

    def test_connection(self) -> tuple[bool, str]:
        """探测连接是否可用，返回 (是否成功, 说明)。非流式、低 token。"""
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
                temperature=0,
            )
            ok = bool(resp.choices and resp.choices[0].message)
            return ok, "连接成功" if ok else "连接无响应"
        except Exception as e:
            return False, f"连接失败：{type(e).__name__}: {e}"

    def list_available_models(self) -> list[str]:
        """拉取当前 provider 的可用模型列表（UI 下拉选择用）。

        对标 Hermes `providers/base.py::fetch_models()`；任何失败都返回空列表，不抛异常。
        """
        try:
            return list_provider_models(
                self.provider,
                api_key=self.client.api_key,
                base_url=str(self.client.base_url),
            )
        except Exception:
            return []

    def chat(
        self,
        messages: list[dict],
        stream: bool = False,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        tools: Optional[list[dict]] = None,
    ) -> ChatCompletion | Stream[ChatCompletionChunk]:
        """发送聊天请求

        tools: OpenAI/DeepSeek 兼容的 function-calling 工具定义列表。
        传入后自动启用 tool_choice="auto"，模型可自主决定调用工具。
        """
        kwargs = dict(
            model=self.model,
            messages=messages,
            stream=stream,
            temperature=temperature,
        )
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        # 推理/思考模式透传（P0）：把 reasoning 参数转发给支持的模型
        # （OpenAI o1/o3、深度推理本地模型等；不同后端语义不同，可用
        #  REASONING_EXTRA_BODY 自定义 extra_body JSON 覆盖默认）。
        if settings.REASONING_ENABLED:
            if settings.REASONING_EXTRA_BODY:
                kwargs["extra_body"] = dict(settings.REASONING_EXTRA_BODY)
            else:
                kwargs["extra_body"] = {"reasoning_effort": settings.REASONING_EFFORT}
        return self.client.chat.completions.create(**kwargs)

    def chat_stream(self, messages: list[dict], **kwargs):
        """流式聊天"""
        return self.chat(messages, stream=True, **kwargs)

    def describe_image(self, images: list[str], prompt: str = "请描述这张图片的内容，提取其中的关键信息。") -> str:
        """视觉回退（Hermes 的 vision_analyze 思路）：

        当主模型不支持视觉时，用辅助视觉模型把图片转成文字描述。
        图片以 base64 data URL 或 http(s) URL 传入。返回文字描述；
        若未配置辅助视觉模型或无可用后端，返回空串（调用方据此提示用户）。
        """
        model = settings.AUX_VISION_MODEL or self.model
        if not model:
            return ""
        try:
            content: list[dict] = [{"type": "text", "text": prompt}]
            for img in images:
                content.append({"type": "image_url", "image_url": {"url": img}})
            resp = self.client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": content}],
                max_tokens=512,
                temperature=0.3,
            )
            return resp.choices[0].message.content or ""
        except Exception as e:
            logger.warning(f"图片视觉描述失败：{e}")
            return ""