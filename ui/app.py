"""AI-Companion Web 界面 — 基于 Streamlit

集成了：
- 普通聊天（默认模式）
- 角色扮演（切换人物视角）
- 女娲蒸馏（创建新人物）
- 知识库管理（查看/搜索记忆精华）
"""

import json
import os
import tempfile
from pathlib import Path

import streamlit as st

# 确保页面配置在最前面
st.set_page_config(
    page_title="AI-Companion · 智能伴侣",
    page_icon="🐾",
    layout="wide",
    initial_sidebar_state="expanded",
)

from core.agent import Agent
from skills.actor import Actor
from skills.nuwa import Nuwa
from skills.loader import load_all_perspectives


# ========== 初始化 ==========

@st.cache_resource
def init_agent():
    return Agent()

@st.cache_resource
def init_nuwa():
    return Nuwa()

@st.cache_resource
def init_actor():
    return Actor()

@st.cache_resource
def init_registry():
    return load_all_perspectives()


agent = init_agent()
nuwa = init_nuwa()
actor = init_actor()
registry = init_registry()


# ========== Sidebar ==========

with st.sidebar:
    st.markdown("# 🐾 AI-Companion")
    st.markdown("**智能伴侣 · 角色扮演**")

    # 模式选择
    mode = st.radio(
        "对话模式",
        ["💬 普通聊天", "🎭 角色扮演", "🔮 女娲造人", "📚 知识库"],
        index=0,
    )

    st.divider()

    if mode == "🎭 角色扮演":
        st.markdown("### 可选人物")
        # 搜索框
        search = st.text_input("🔍 搜索人物", placeholder="输入人名...")
        characters = actor.search_characters(search) if search else actor.list_characters()
        # 按来源排序：内置在前，自定义在后
        characters.sort(key=lambda c: (0 if c["source"] == "builtin" else 1, c["name_en"]))

        # 分组显示
        cols = st.columns(2)
        for i, ch in enumerate(characters[:12]):
            with cols[i % 2]:
                label = f"{ch['name']} ({ch['name_en']})"
                if st.button(label, key=f"char_{ch['id']}", use_container_width=True):
                    success, msg = actor.activate(ch["id"])
                    if success:
                        st.success(msg)
                        st.rerun()
                    else:
                        st.error(msg)

    if actor.active:
        active_id = actor.active
        p = registry.get(active_id)
        if p:
            st.info(f"🎭 当前扮演：**{p.name}**")
        if st.button("❌ 退出角色", use_container_width=True):
            msg = actor.deactivate()
            st.success(msg)
            st.rerun()

    st.divider()
    st.caption("Powered by AI-Companion")


# ========== 主界面 ==========

if mode == "💬 普通聊天":
    st.markdown("# 💬 和小伴聊天")

    # 初始化聊天历史
    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []
    if "session_id" not in st.session_state:
        import uuid
        st.session_state.session_id = str(uuid.uuid4())

    # 显示聊天历史
    for msg in st.session_state.chat_history:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # 输入框
    if prompt := st.chat_input("说说你的想法..."):
        if actor.active:
            st.info(f"🎭 当前以 {actor.active} 模式回答", icon="🎭")

        st.session_state.chat_history.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            with st.spinner("思考中..."):
                reply = agent.chat(st.session_state.session_id, prompt)
            st.markdown(reply)

        st.session_state.chat_history.append({"role": "assistant", "content": reply})

elif mode == "🎭 角色扮演":
    st.markdown("# 🎭 角色扮演模式")

    if actor.active:
        p = registry.get(actor.active)
        st.success(f"当前扮演：**{p.name}**（{p.name_en}）")
        st.markdown(f"> {p.description}")

        # 角色聊天
        if "role_chat_history" not in st.session_state:
            st.session_state.role_chat_history = []
        if "role_session_id" not in st.session_state:
            import uuid
            st.session_state.role_session_id = str(uuid.uuid4())

        for msg in st.session_state.role_chat_history:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])

        if prompt := st.chat_input(f"对{p.name}说点什么..."):
            st.session_state.role_chat_history.append({"role": "user", "content": prompt})
            with st.chat_message("user"):
                st.markdown(prompt)

            with st.chat_message("assistant", avatar="🎭"):
                with st.spinner(f"{p.name}在思考..."):
                    reply = agent.chat(st.session_state.role_session_id, prompt)
                st.markdown(reply)

            st.session_state.role_chat_history.append({"role": "assistant", "content": reply})
    else:
        st.info("👈 请在左侧边栏选择一个人物开始角色扮演")

elif mode == "🔮 女娲造人":
    st.markdown("# 🔮 女娲造人")
    st.markdown("输入一个人名或主题，自动调研→思维框架提炼→生成可运行的人物视角。")

    col1, col2 = st.columns([2, 1])

    with col1:
        person_input = st.text_input(
            "输入人名/主题",
            placeholder="例如：巴菲特、达芬奇、阿德勒..."
        )

        materials = st.text_area(
            "提供素材（可选）",
            placeholder="如果这个人比较冷门，可以提供你知道的资料、文章链接或背景信息..."
        )

        if st.button("🔮 开始蒸馏", type="primary", use_container_width=True):
            if not person_input.strip():
                st.warning("请输入人名")
            else:
                with st.spinner(f"正在调研和提炼「{person_input}」的思维框架..."):
                    result = nuwa.distill(person_input, materials)
                st.success("蒸馏完成！")
                st.markdown(result)

    with col2:
        st.markdown("### 什么是女娲？")
        st.markdown("""
        女娲不是复制人，是**提炼思维框架**。

        一个好的人物Skill是一套可运行的认知操作系统：
        - **心智模型** — 他怎么看世界？
        - **决策启发式** — 他怎么快速判断？
        - **表达DNA** — 他怎么说话？
        - **反模式** — 他绝对不做什么？

        捕捉的是 **HOW they think**，不是WHAT they said。
        """)

    st.divider()

    # 诊断模式
    st.markdown("### 不知道要蒸馏谁？")
    demand = st.text_input(
        "描述你的需求或困惑",
        placeholder="例如：我想提升决策质量、怎样才能写出好文章..."
    )
    if demand:
        candidates = nuwa.diagnose(demand)
        st.markdown("#### 推荐人物候选")
        for c in candidates:
            with st.container(border=True):
                st.markdown(f"**{c['name']}**（{c['name_en']}）")
                st.markdown(f"> {c['description']}")
                st.caption(f"推荐理由：{c['reason']}")

elif mode == "📚 知识库":
    st.markdown("# 📚 知识库")
    st.markdown("这里存放所有已蒸馏人物的精华对话和思维框架。")

    registry = init_registry()
    characters = registry.list_all()

    if not characters:
        st.info("还没有任何蒸馏记录")
    else:
        # 按来源分列
        builtin = [c for c in characters if c["source"] == "builtin"]
        distilled = [c for c in characters if c["source"] == "distilled"]

        if builtin:
            with st.expander(f"📦 内置人物（{len(builtin)}个）", expanded=True):
                for c in builtin:
                    with st.container(border=True):
                        st.markdown(f"**{c['name']}** — {c['name_en']}")
                        st.markdown(f"> {c['description']}")
                        if c["tags"]:
                            st.caption("标签: " + ", ".join(c["tags"]))

        if distilled:
            with st.expander(f"🔮 自定义蒸馏（{len(distilled)}个）", expanded=True):
                for c in distilled:
                    with st.container(border=True):
                        st.markdown(f"**{c['name']}**")
                        st.markdown(f"> {c['description']}")


# ========== 运行入口 ==========

def run():
    """占位函数，实际由 Streamlit 直接运行此文件"""
    pass


if __name__ == "__main__":
    # Streamlit 直接运行此文件
    pass