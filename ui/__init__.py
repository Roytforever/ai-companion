"""AI-Companion Web 界面 — Streamlit 应用"""

import streamlit as st
import uuid

from core.agent import Agent
from config import settings

# ---------- 页面配置 ----------
st.set_page_config(
    page_title="AI-Companion 智能伴侣",
    page_icon="🐾",
    layout="centered",
    initial_sidebar_state="expanded",
)

# ---------- CSS 美化 ----------
st.markdown("""
<style>
    .stApp {
        max-width: 800px;
        margin: 0 auto;
    }
    .chat-message {
        padding: 1rem;
        border-radius: 12px;
        margin-bottom: 0.5rem;
    }
    .user-msg {
        background-color: #e8f4fd;
        border-left: 4px solid #4a90d9;
    }
    .assistant-msg {
        background-color: #f0f0f0;
        border-left: 4px solid #34a853;
    }
    .stSidebar {
        background-color: #fafafa;
    }
</style>
""", unsafe_allow_html=True)


# ---------- 初始化 ----------
def init_session():
    if "agent" not in st.session_state:
        st.session_state.agent = Agent()
    if "session_id" not in st.session_state:
        st.session_state.session_id = f"session_{uuid.uuid4().hex[:8]}"
    if "messages" not in st.session_state:
        st.session_state.messages = [
            {"role": "assistant", "content": "你好！我是你的智能伴侣 🐾\n\n有什么想聊的吗？我可以：\n- 💬 陪你聊天\n- ⏰ 查询时间日期\n- 🧮 做数学计算\n- 📝 记住你的偏好"}
        ]

init_session()


# ---------- 侧边栏 ----------
with st.sidebar:
    st.markdown("## 🐾 AI-Companion")
    st.caption("你的智能伴侣")

    st.divider()

    # 会话管理
    st.markdown("### 会话管理")
    sessions = st.session_state.agent.memory.list_sessions()
    session_names = [s["session_id"] for s in sessions] if sessions else []
    selected = st.selectbox(
        "切换会话",
        options=[st.session_state.session_id] + session_names,
        index=0,
        key="session_selector",
    )

    if selected != st.session_state.session_id:
        st.session_state.session_id = selected
        history = st.session_state.agent.memory.get_history(selected)
        st.session_state.messages = [
            {"role": "assistant", "content": f"已切换到会话 {selected[:16]}..."}
        ]
        for msg in history:
            st.session_state.messages.append(msg)
        st.rerun()

    # 新建会话
    if st.button("🆕 新会话", use_container_width=True):
        st.session_state.session_id = f"session_{uuid.uuid4().hex[:8]}"
        st.session_state.messages = [
            {"role": "assistant", "content": "新会话已开始！有什么想聊的？"}
        ]
        st.rerun()

    # 清空当前会话
    if st.button("🗑️ 清空当前对话", use_container_width=True):
        st.session_state.agent.memory.clear_history(st.session_state.session_id)
        st.session_state.messages = [
            {"role": "assistant", "content": "对话已清空！"}
        ]
        st.rerun()

    st.divider()

    # 关于
    st.markdown("### ℹ️ 关于")
    st.caption(
        f"模型：{settings.LLM_MODEL}\n\n"
        "一个具备记忆和工具调用能力的 AI Agent 练手项目。"
    )


# ---------- 主聊天区域 ----------
# 显示历史消息
for msg in st.session_state.messages:
    css_class = "user-msg" if msg["role"] == "user" else "assistant-msg"
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# 输入框
if prompt := st.chat_input("说说你想聊什么..."):
    # 显示用户消息
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    # 获取 AI 回复
    with st.chat_message("assistant"):
        with st.spinner("小伴思考中..."):
            try:
                reply = st.session_state.agent.chat(
                    st.session_state.session_id, prompt
                )
                st.markdown(reply)
                st.session_state.messages.append(
                    {"role": "assistant", "content": reply}
                )
            except ValueError as e:
                st.error(str(e))
                st.info(
                    "💡 请先在 `.env` 文件中配置你的 API Key，"
                    "或点击侧边栏的设置查看说明。"
                )
            except Exception as e:
                st.error(f"出错了：{e}")