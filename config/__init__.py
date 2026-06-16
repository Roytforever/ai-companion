# 配置管理

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


class Settings:
    """应用配置"""

    # API
    LLM_API_KEY: str = os.getenv("LLM_API_KEY", "")
    LLM_BASE_URL: str = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
    LLM_MODEL: str = os.getenv("LLM_MODEL", "deepseek-chat")

    # Memory
    MEMORY_STORE: str = os.getenv("MEMORY_STORE", "local")
    MEMORY_DIR: Path = Path(__file__).parent.parent / "memory" / "data"

    # App
    APP_NAME: str = os.getenv("APP_NAME", "AI-Companion")
    APP_LANG: str = os.getenv("APP_LANG", "zh-CN")

    @property
    def llm_client_kwargs(self) -> dict:
        return {
            "api_key": self.LLM_API_KEY,
            "base_url": self.LLM_BASE_URL,
        }


settings = Settings()