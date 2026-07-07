# 配置管理

import json
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

    # 模型后端：cloud（OpenAI/DeepSeek 等兼容接口）| local（Ollama 等本地服务）
    LLM_BACKEND: str = os.getenv("LLM_BACKEND", "cloud")
    # 默认模型提供商（provider 注册表名，见 core/providers.py）；空则按后端推导
    # （cloud→deepseek, local→ollama）。可在 .env 显式指定，如 LLM_PROVIDER=openai
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "")
    # 模型提供商插件目录（Hermes 式 plugins/ 扩展点：把 *.py 丢进去即注册新提供商）
    PROVIDER_PLUGINS_DIR: Path = Path(os.getenv("PROVIDER_PLUGINS_DIR", "")) or (
        Path(__file__).parent.parent / "provider_plugins"
    )
    LLM_TIMEOUT: float = float(os.getenv("LLM_TIMEOUT", "60"))
    LOCAL_MODEL_BASE_URL: str = os.getenv("LOCAL_MODEL_BASE_URL", "http://localhost:11434/v1")
    LOCAL_MODEL_NAME: str = os.getenv("LOCAL_MODEL_NAME", "llama3")

    # 视觉/多模态（Phase 4）
    # 主模型是否支持视觉：None=按模型名自动推断；True/False=强制
    SUPPORTS_VISION: str = os.getenv("SUPPORTS_VISION", "")
    # 辅助视觉模型（主模型无视觉能力时，用于把图片转成文字描述）
    AUX_VISION_MODEL: str = os.getenv("AUX_VISION_MODEL", "")

    # MCP 服务器配置（Phase 4）：dict[server_name] -> {command/args/env 或 url/headers}
    # 也可经环境变量 MCP_SERVERS 传入 JSON 字符串
    MCP_SERVERS: dict = {}
    _mcp_env = os.getenv("MCP_SERVERS", "")
    if _mcp_env:
        try:
            MCP_SERVERS = json.loads(_mcp_env)
        except Exception:
            MCP_SERVERS = {}

    # 文件读取允许的根目录（Phase 4，防任意读）：默认仅项目根
    _file_roots_env = os.getenv("FILE_READ_ROOTS", "")
    FILE_READ_ROOTS: list = (
        [p for p in _file_roots_env.split(os.pathsep) if p]
        if _file_roots_env
        else [str(Path(__file__).parent.parent)]
    )

    # Skills Hub 来源（Phase 5）：JSON 列表，元素见 core/skills_hub.py
    # 形如 [{"type":"local","path":"..."},{"type":"github","repo":"owner/repo","ref":"main","subdir":"skills"},
    #        {"type":"url","url":"https://.../index.json"}]
    SKILLS_HUB_SOURCES: list = []
    _hub_env = os.getenv("SKILLS_HUB_SOURCES", "")
    if _hub_env:
        try:
            SKILLS_HUB_SOURCES = json.loads(_hub_env)
        except Exception:
            SKILLS_HUB_SOURCES = []

    # MCP OAuth 令牌缓存目录（Phase 5，gitignore）：默认项目根下的 .mcp_tokens
    MCP_TOKEN_DIR: Path = Path(os.getenv("MCP_TOKEN_DIR", "")) or (
        Path(__file__).parent.parent / ".mcp_tokens"
    )

    # Hermes 主目录（Phase 5）：SOUL.md 等全局身份仅从此加载（对齐 Hermes HERMES_HOME）
    HERMES_HOME: Path = Path(os.getenv("HERMES_HOME", "")) or (Path.home() / ".hermes")

    # RL 轨迹 / 导出数据目录（Phase 5，gitignore）：默认项目根下的 data/rl
    RL_DATA_DIR: Path = Path(os.getenv("RL_DATA_DIR", "")) or (
        Path(__file__).parent.parent / "data" / "rl"
    )

    # ============ P0：权限 / 执行 / 压缩 / 推理 ============
    # 工具权限模式：ask(默认, 危险工具需授权) / allow(全部放行) / deny(全部拒绝)
    TOOL_PERMISSION_MODE: str = os.getenv("TOOL_PERMISSION_MODE", "ask")
    # 是否启用权限系统（false 则等同 allow）
    TOOL_PERMISSIONS_ENABLED: bool = os.getenv("TOOL_PERMISSIONS_ENABLED", "true").lower() != "false"
    # shell 执行命令白名单（逗号分隔），空则用内置默认安全清单
    SHELL_ALLOWLIST: str = os.getenv("SHELL_ALLOWLIST", "")
    # 上下文压缩阈值（估算 token）；超阈值时旧轮次被摘要
    CONTEXT_COMPACT_THRESHOLD: int = int(os.getenv("CONTEXT_COMPACT_THRESHOLD", "6000"))
    # 推理/思考模式：是否透传 reasoning 参数给支持该能力的模型（如 o1/o3/深度推理本地模型）
    REASONING_ENABLED: bool = os.getenv("REASONING_ENABLED", "false").lower() in ("1", "true", "yes", "y")
    REASONING_EFFORT: str = os.getenv("REASONING_EFFORT", "medium")
    # 自定义 reasoning extra_body（JSON 字符串），优先级高于默认；不同后端语义不同，自行配置
    _reasoning_body_env = os.getenv("REASONING_EXTRA_BODY", "")
    REASONING_EXTRA_BODY: dict = {}
    if _reasoning_body_env:
        try:
            REASONING_EXTRA_BODY = json.loads(_reasoning_body_env)
        except Exception:
            REASONING_EXTRA_BODY = {}

    # ============ P1：子智能体 + 定时调度 ============
    # 子智能体最大递归深度（防止 token 爆炸 / 无限委派）
    SUBAGENT_MAX_DEPTH: int = int(os.getenv("SUBAGENT_MAX_DEPTH", "3"))
    # 单个子智能体最大工具循环轮数
    SUBAGENT_MAX_ITER: int = int(os.getenv("SUBAGENT_MAX_ITER", "4"))
    # MoA 默认并行的「参考顾问」数量
    MOA_DEFAULT_REFERENCES: int = int(os.getenv("MOA_DEFAULT_REFERENCES", "2"))
    # MoA 真实多模型池：顾问模型列表（"provider:model" 逗号分隔，如
    # "deepseek:deepseek-chat, qwen:qwen-plus, openai:gpt-4o-mini"）。
    # 留空则回退「同模型多视角」模式（仅 prompt 视角不同，复用主模型）。
    MOA_ADVISOR_MODELS: str = os.getenv("MOA_ADVISOR_MODELS", "")
    # MoA 聚合者模型（"provider:model"）。留空则用主模型（保留完整工具能力）；
    # 指定则用该模型做单轮综合。
    MOA_AGGREGATOR: str = os.getenv("MOA_AGGREGATOR", "")
    # 调度器是否启用（后台 tick 线程）
    SCHEDULER_ENABLED: bool = os.getenv("SCHEDULER_ENABLED", "true").lower() != "false"
    # 调度器 tick 周期（秒），对齐 Hermes 默认 60s
    SCHEDULER_TICK_SECONDS: int = int(os.getenv("SCHEDULER_TICK_SECONDS", "60"))
    # 调度器数据目录（jobs.json / inbox.json / output/，gitignore）
    SCHEDULER_DATA_DIR: Path = Path(os.getenv("SCHEDULER_DATA_DIR", "")) or (
        Path(__file__).parent.parent / "data" / "schedules"
    )

    # ============ P1b：hooks / 斜杠命令 / 浏览器 / 辩证用户建模 ============
    # 事件钩子（对齐 Hermes Hooks）
    HOOKS_ENABLED: bool = os.getenv("HOOKS_ENABLED", "true").lower() != "false"
    HOOKS_DIR: Path = Path(os.getenv("HOOKS_DIR", "")) or (
        Path(__file__).parent.parent / ".hooks"
    )
    # 钩子插件目录（Python Plugin 动态注册：.hooks/plugins/*.py）
    HOOKS_PLUGINS_DIR: Path = Path(os.getenv("HOOKS_PLUGINS_DIR", "")) or (HOOKS_DIR / "plugins")
    # 斜杠命令目录（commands/<name>.md）
    SLASH_COMMANDS_DIR: Path = Path(os.getenv("SLASH_COMMANDS_DIR", "")) or (
        Path(__file__).parent.parent / "commands"
    )
    # bundle 自动激活的 context 目录（install_bundle 把 bundle 的 context/*.md 复制到这里）
    BUNDLE_CONTEXT_DIR: Path = Path(os.getenv("BUNDLE_CONTEXT_DIR", "")) or (
        Path(__file__).parent.parent / "context"
    )
    # 浏览器后端：auto(默认，自动探测 Playwright) / playwright / fetch(纯网页读取)
    BROWSER_BACKEND: str = os.getenv("BROWSER_BACKEND", "auto")
    BROWSER_TIMEOUT: int = int(os.getenv("BROWSER_TIMEOUT", "30"))
    # 浏览器截图保存目录（仅 Playwright 截图动作使用）
    BROWSER_SCREENSHOT_DIR: Path = Path(os.getenv("BROWSER_SCREENSHOT_DIR", "")) or (
        Path(__file__).parent.parent / "screenshots"
    )

    # Memory
    MEMORY_STORE: str = os.getenv("MEMORY_STORE", "local")
    MEMORY_DIR: Path = Path(__file__).parent.parent / "memory" / "data"

    # App
    APP_NAME: str = os.getenv("APP_NAME", "AI-Companion")
    APP_LANG: str = os.getenv("APP_LANG", "zh-CN")

    @property
    def llm_client_kwargs(self) -> dict:
        """根据后端选择返回对应的连接参数。"""
        if self.LLM_BACKEND == "local":
            return {
                "api_key": self.LLM_API_KEY or "ollama",  # 本地服务通常不需要真实 key
                "base_url": self.LOCAL_MODEL_BASE_URL,
                "model": self.LOCAL_MODEL_NAME,
                "timeout": self.LLM_TIMEOUT,
            }
        return {
            "api_key": self.LLM_API_KEY,
            "base_url": self.LLM_BASE_URL,
            "model": self.LLM_MODEL,
            "timeout": self.LLM_TIMEOUT,
        }

    # ---- 视觉/多模态（Phase 4）----
    # 按模型名启发式判断视觉能力的关键词（覆盖主流视觉模型；DeepSeek 等纯文本不在内）
    _VISION_HINTS = (
        "vision", "-vl", "vl-", "gpt-4o", "gpt-4-turbo", "claude", "gemini",
        "llava", "pixtral", "glm-4v", "glm-v", "glm-5v", "internvl",
        "minicpm", "yi-vl", "moondream", "step-1v", "qwen-vl", "qwen2-vl",
        "bakllava", "llama3.2-vision",
    )

    @staticmethod
    def model_supports_vision(model_name: str) -> bool:
        """按模型名启发式判断是否支持视觉（图片输入）。"""
        n = (model_name or "").lower()
        return any(h in n for h in Settings._VISION_HINTS)

    @property
    def supports_vision(self) -> bool:
        """当前生效模型是否支持视觉。

        SUPPORTS_VISION 环境变量强制（true/false）；未设置则按当前模型名自动推断。
        """
        v = (self.SUPPORTS_VISION or "").strip().lower()
        if v in ("true", "1", "yes", "y"):
            return True
        if v in ("false", "0", "no", "n"):
            return False
        model = self.LOCAL_MODEL_NAME if self.LLM_BACKEND == "local" else self.LLM_MODEL
        return Settings.model_supports_vision(model)


settings = Settings()