"""工具注册中心"""

from typing import Any, Callable, get_type_hints
import inspect


class Tool:
    """工具描述"""

    def __init__(
        self,
        name: str,
        description: str,
        fn: Callable,
        parameters: dict[str, dict] | None = None,
    ):
        self.name = name
        self.description = description
        self.fn = fn
        self.parameters = parameters or self._infer_params(fn)

    def _infer_params(self, fn: Callable) -> dict[str, dict]:
        """从函数签名推断参数（Python 类型名 → JSON Schema 类型名）

        OpenAI/DeepSeek function calling 要求 JSON Schema 类型名
        （string/integer/number/boolean/array/object），而 Python 的
        __name__ 返回的是 str/int 等。这里做映射，否则工具会被静默忽略。
        """
        type_map = {
            "str": "string",
            "int": "integer",
            "float": "number",
            "bool": "boolean",
            "list": "array",
            "dict": "object",
        }
        sig = inspect.signature(fn)
        hints = get_type_hints(fn)
        params = {}
        for pname, param in sig.parameters.items():
            if pname == "return":
                continue
            py_type = hints.get(pname, str).__name__
            json_type = type_map.get(py_type, "string")
            desc = {"type": json_type, "description": f"参数 {pname}"}
            if param.default is inspect.Parameter.empty:
                desc["required"] = True
            params[pname] = desc
        return params

    def run(self, **kwargs) -> Any:
        """执行工具"""
        return self.fn(**kwargs)

    def to_openai_tool(self) -> dict:
        """转为 OpenAI 工具格式"""
        properties = {}
        required = []
        for pname, info in self.parameters.items():
            properties[pname] = {
                "type": info.get("type", "string"),
                "description": info.get("description", ""),
            }
            if info.get("required"):
                required.append(pname)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }


class ToolRegistry:
    """工具注册中心"""

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, name: str = "", description: str = ""):
        """装饰器：注册工具"""

        def decorator(fn):
            tool = Tool(
                name=name or fn.__name__,
                description=description or fn.__doc__ or "",
                fn=fn,
            )
            self._tools[tool.name] = tool
            return fn

        return decorator

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def list_tools(self) -> list[Tool]:
        return list(self._tools.values())

    def to_openai_tools(self) -> list[dict]:
        return [t.to_openai_tool() for t in self._tools.values()]


# 全局实例
registry = ToolRegistry()
