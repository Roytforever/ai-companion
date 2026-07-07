"""大模型调用封装 —— 兼容层。

LLMClient 的实现位于 core/__init__.py（包入口），这里仅做 re-export，
保持 `from core.llm import LLMClient` 这种旧导入路径仍可工作。
"""

from core import LLMClient

__all__ = ["LLMClient"]
