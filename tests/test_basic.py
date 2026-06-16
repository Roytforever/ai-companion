"""运行测试"""

from tests.test_basic import test_memory_store, test_tool_registry

if __name__ == "__main__":
    test_memory_store()
    test_tool_registry()
    print("\n🎉 全部测试通过！")