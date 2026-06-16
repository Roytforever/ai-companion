"""AI-Companion 单元测试"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from memory.store import MemoryStore
from tools.registry import Tool, ToolRegistry


def test_memory_store():
    """测试记忆存储基本功能"""
    store = MemoryStore()
    store.save_message("test_session", "user", "你好")
    store.save_message("test_session", "assistant", "你好！有什么需要帮助的吗？")
    history = store.get_history("test_session")
    assert len(history) == 2
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "你好"
    # 清理
    store.clear_history("test_session")
    assert len(store.get_history("test_session")) == 0
    print("✅ 记忆存储测试通过")


def test_tool_registry():
    """测试工具注册和执行"""
    registry = ToolRegistry()

    @registry.register(name="hello", description="说你好")
    def hello(name: str):
        return f"你好，{name}！"

    tool = registry.get("hello")
    assert tool is not None
    assert tool.name == "hello"
    assert tool.run(name="小明") == "你好，小明！"
    print("✅ 工具注册测试通过")


if __name__ == "__main__":
    test_memory_store()
    test_tool_registry()
    print("\n🎉 所有测试通过！")