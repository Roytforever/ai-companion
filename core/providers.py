"""模型提供商注册表 —— 对齐 Hermes 的 `providers/` + `fetch_models()` 能力（插件化版）。

设计目标（参照 Hermes）：
- 把「模型提供商」做成一等公民：**新增一个提供商 = 丢一个 .py 插件进 provider_plugins/ 目录**，
  不改 LLMClient 核心逻辑（对齐 Hermes 的 plugins/model-providers 思路）。
- 内置 7 个开箱即用（deepseek / openai / qwen / moonshot / openrouter / ollama / custom），
  写在 `BUILTIN_PROVIDERS`；用户扩展放 `PROVIDER_PLUGINS_DIR`（默认项目根 `provider_plugins/`），
  启动时自动加载并与内置合并（同名插件可覆盖内置，实现定制）。
- `list_provider_models()` 对标 Hermes `providers/base.py::fetch_models()`：
  动态拉取 `/models` 端点，让 UI 能「拉取可用模型」做下拉选择；失败则回退静态列表。
- 零新依赖：仅用标准库 urllib 发请求。

## 插件模板（丢进 provider_plugins/myai.py 即生效）

    from core.providers import ProviderSpec

    PROVIDER_SPEC = ProviderSpec(
        name="myai",                       # 注册表键（唯一）
        label="My AI Gateway",
        base_url="https://my.ai/v1",
        api_key_env="MYAI_API_KEY",        # 从环境变量读 key
        default_model="my-model-x",
        supports_reasoning=False,
        supports_vision=True,
        models=["my-model-x", "my-model-y"],   # 留空则走动态拉取
    )

或者用函数式注册（支持一次注册多个 / 自定义逻辑）：

    from core.providers import ProviderSpec

    def register(registry: dict):
        registry["myai"] = ProviderSpec(name="myai", label="My AI",
                                        base_url="https://my.ai/v1",
                                        api_key_env="MYAI_API_KEY",
                                        default_model="my-model-x")

## 内置提供商（均 OpenAI 兼容接口）
  deepseek / openai / qwen(通义) / moonshot(Kimi) / openrouter / ollama(本地) / custom(自定义)
"""

import importlib.util
import json
import logging
import os
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)


@dataclass
class ProviderSpec:
    """一个模型提供商的描述"""
    name: str                     # 注册表键，如 "deepseek"
    label: str                    # 展示名，如 "DeepSeek"
    base_url: Optional[str]       # API base（None = 用 settings.LLM_BASE_URL，给 custom 用）
    api_key_env: Optional[str]    # 读取 key 的环境变量名（None = 用 settings.LLM_API_KEY）
    default_model: str = ""       # 默认模型
    supports_reasoning: bool = False   # 该提供商是否支持推理/思考模式
    supports_vision: bool = False      # 该提供商默认是否有视觉能力（仅作提示，真相以模型名为准）
    models: list = field(default_factory=list)  # 静态模型列表（空时走动态拉取）
    local: bool = False           # 是否本地服务（无需真实 key）


# base_url 为 None 表示「自定义 OpenAI 兼容端点」，base 用 settings.LLM_BASE_URL
BUILTIN_PROVIDERS: dict[str, ProviderSpec] = {
    "deepseek": ProviderSpec(
        name="deepseek", label="DeepSeek",
        base_url="https://api.deepseek.com/v1", api_key_env="LLM_API_KEY",
        default_model="deepseek-chat", supports_reasoning=True, supports_vision=False,
        models=["deepseek-chat", "deepseek-reasoner"],
    ),
    "openai": ProviderSpec(
        name="openai", label="OpenAI",
        base_url="https://api.openai.com/v1", api_key_env="OPENAI_API_KEY",
        default_model="gpt-4o-mini", supports_reasoning=True, supports_vision=True,
        models=["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "o1-mini", "o3-mini"],
    ),
    "qwen": ProviderSpec(
        name="qwen", label="通义千问 (Qwen)",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key_env="DASHSCOPE_API_KEY",
        default_model="qwen-plus", supports_reasoning=False, supports_vision=True,
        models=["qwen-plus", "qwen-max", "qwen-turbo", "qwen-vl-plus", "qwen2.5-72b-instruct"],
    ),
    "moonshot": ProviderSpec(
        name="moonshot", label="Kimi (Moonshot)",
        base_url="https://api.moonshot.cn/v1", api_key_env="MOONSHOT_API_KEY",
        default_model="moonshot-v1-8k", supports_reasoning=True, supports_vision=False,
        models=["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
    ),
    "openrouter": ProviderSpec(
        name="openrouter", label="OpenRouter (聚合)",
        base_url="https://openrouter.ai/api/v1", api_key_env="OPENROUTER_API_KEY",
        default_model="openai/gpt-4o-mini", supports_reasoning=True, supports_vision=True,
        models=[],  # 聚合平台模型极多，默认走动态拉取
    ),
    "ollama": ProviderSpec(
        name="ollama", label="Ollama (本地)",
        base_url="http://localhost:11434/v1", api_key_env=None,
        default_model="llama3", supports_reasoning=False, supports_vision=False,
        models=[], local=True,  # 本地模型列表动态拉取
    ),
    "custom": ProviderSpec(
        name="custom", label="自定义 OpenAI 兼容",
        base_url=None, api_key_env="LLM_API_KEY",
        default_model="", supports_reasoning=False, supports_vision=False,
        models=[],
    ),
}


# ---------------- 插件动态加载（Hermes 式 providers/ 扩展点） ----------------

_PLUGIN_CACHE: Optional[dict] = None


def _import_plugin(path: Path):
    """用 importlib 从任意路径加载一个插件模块（模块名唯一，避免冲突）。"""
    mod_name = f"_provider_plugin_{path.stem}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_provider_plugins(plugins_dir=None) -> dict:
    """扫描插件目录，返回 {name: ProviderSpec}。

    支持两种约定（任一即可）：
    - 模块级变量 `PROVIDER_SPEC = ProviderSpec(...)`
    - 函数 `register(registry: dict)`（可一次注册多个 / 带自定义逻辑）
    以下划线开头的文件被跳过（可作模板/草稿）。任何加载异常都被记录并忽略，
    不拖垮主程序（对齐 Hermes 非阻塞插件加载）。
    """
    d = Path(plugins_dir) if plugins_dir else settings.PROVIDER_PLUGINS_DIR
    out: dict = {}
    if not d or not d.exists():
        return out
    for path in sorted(d.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            mod = _import_plugin(path)
        except Exception as e:
            logger.warning(f"Provider 插件加载失败 {path.name}：{e}")
            continue
        # 约定 A：模块级 PROVIDER_SPEC
        spec = getattr(mod, "PROVIDER_SPEC", None)
        if isinstance(spec, ProviderSpec):
            out[spec.name] = spec
            continue
        # 约定 B：register(registry)
        reg = getattr(mod, "register", None)
        if callable(reg):
            try:
                reg(out)
            except Exception as e:
                logger.warning(f"Provider 插件 register() 失败 {path.name}：{e}")
    return out


def reload_provider_plugins():
    """清空插件缓存，下次 _all_providers() 重新扫描目录（热重载）。"""
    global _PLUGIN_CACHE
    _PLUGIN_CACHE = None


def _all_providers() -> dict:
    """合并内置 + 动态插件（插件可覆盖同名内置）。带缓存。"""
    global _PLUGIN_CACHE
    if _PLUGIN_CACHE is None:
        _PLUGIN_CACHE = load_provider_plugins()
    merged = {name: p for name, p in BUILTIN_PROVIDERS.items()}
    merged.update(_PLUGIN_CACHE)  # 动态插件覆盖内置
    return merged


def list_providers() -> list[ProviderSpec]:
    """返回所有已注册提供商（供 UI 下拉，含内置 + 插件）"""
    return list(_all_providers().values())


def get_provider(name: str) -> Optional[ProviderSpec]:
    return _all_providers().get(name)


def resolve_provider(name: str, model: str | None = None):
    """解析提供商 → (base_url, api_key, model, supports_reasoning, supports_vision)。

    供 LLMClient 在构造时调用；任一环节失败都抛异常，由调用方回退默认配置。
    """
    spec = get_provider(name)
    if spec is None:
        raise ValueError(f"未知提供商：{name}")
    base_url = spec.base_url or settings.LLM_BASE_URL
    if spec.local:
        api_key = settings.LLM_API_KEY or "ollama"
    elif spec.api_key_env:
        api_key = os.getenv(spec.api_key_env) or settings.LLM_API_KEY
    else:
        api_key = settings.LLM_API_KEY
    resolved_model = model or spec.default_model or settings.LLM_MODEL
    return base_url, api_key, resolved_model, spec.supports_reasoning, spec.supports_vision


def list_provider_models(
    name: str, api_key: str | None = None, base_url: str | None = None
) -> list[str]:
    """动态拉取某提供商的可用模型列表（对标 Hermes fetch_models）。

    优先 GET `{base_url}/models`；解析 OpenAI 形状（data[].id）或 Ollama 形状（models[].name）。
    任何失败/空结果都回退到提供商静态列表或默认模型——永不抛异常，保证 UI 不崩。
    """
    spec = get_provider(name)
    if not spec:
        return []
    url = (base_url or spec.base_url or settings.LLM_BASE_URL).rstrip("/") + "/models"
    key = api_key or (os.getenv(spec.api_key_env) if spec.api_key_env else None) or settings.LLM_API_KEY
    req = urllib.request.Request(url)
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return list(spec.models or ([spec.default_model] if spec.default_model else []))
    models: list[str] = []
    if isinstance(data, dict):
        if isinstance(data.get("data"), list):
            models = [m.get("id") for m in data["data"] if isinstance(m, dict) and m.get("id")]
        elif isinstance(data.get("models"), list):
            # Ollama 形状：models[].name
            models = [
                (m.get("name") or m.get("id"))
                for m in data["models"] if isinstance(m, dict) and (m.get("name") or m.get("id"))
            ]
    if not models:
        return list(spec.models or ([spec.default_model] if spec.default_model else []))
    return models
