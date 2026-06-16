"""内置工具集合"""

from datetime import datetime
from tools.registry import registry


@registry.register(name="get_time", description="获取当前日期和时间")
def get_time():
    """返回当前本地时间"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@registry.register(name="calculate", description="执行数学计算")
def calculate(expression: str):
    """执行简单的数学计算，传入表达式如 '1 + 2 * 3'"""
    try:
        result = eval(expression, {"__builtins__": {}}, {})
        return f"{expression} = {result}"
    except Exception as e:
        return f"计算错误：{e}"


@registry.register(name="echo", description="回显用户输入，用于测试")
def echo(message: str):
    """原样返回输入内容"""
    return message