"""AI-Companion Web 界面 — 基于 Streamlit

集成了：
- 普通聊天（默认模式）
- 角色扮演（切换人物视角）
- 女娲蒸馏（创建新人物）
- 知识库管理（查看/搜索记忆精华）
"""

import json
import os
import base64
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

from config import settings
from core.agent import Agent
from core.providers import list_providers, get_provider, list_provider_models
from core.feedback import FeedbackStore
from skills.actor import Actor
from skills.nuwa import Nuwa
from skills.loader import load_all_perspectives


# ========== 初始化 ==========

if "llm_provider" not in st.session_state:
    st.session_state.llm_provider = (
        settings.LLM_PROVIDER
        or ("ollama" if settings.LLM_BACKEND == "local" else "deepseek")
    )
if "llm_model" not in st.session_state:
    st.session_state.llm_model = (
        settings.LOCAL_MODEL_NAME if settings.LLM_BACKEND == "local" else settings.LLM_MODEL
    )


@st.cache_resource
def init_agent(provider: str = None, model: str = None, backend: str = None):
    return Agent(provider=provider, model=model, backend=backend)

@st.cache_resource
def init_nuwa():
    return Nuwa()

@st.cache_resource
def init_actor():
    return Actor()

@st.cache_resource
def init_registry():
    return load_all_perspectives()


agent = init_agent(st.session_state.llm_provider, st.session_state.llm_model)
nuwa = init_nuwa()
actor = init_actor()
registry = init_registry()
feedback_store = FeedbackStore()


# ========== 反馈与主动对话辅助 ==========

def _last_user_prompt(history: list, idx: int) -> str:
    """找到 idx 之前最近的一条用户消息内容。"""
    for j in range(idx - 1, -1, -1):
        if history[j]["role"] == "user":
            return history[j]["content"]
    return ""


def render_assistant_feedback(idx: int, history: list, session_id: str, history_key: str):
    """在助手消息下渲染 👍/👎 反馈控件（P2-3 DPO 埋点）。"""
    msg = history[idx]
    if msg.get("role") != "assistant":
        return
    c1, c2 = st.columns([1, 1])
    if c1.button("👍", key=f"up_{history_key}_{idx}"):
        fid = feedback_store.record(
            session_id, _last_user_prompt(history, idx), msg["content"], "up"
        )
        msg["feedback_id"] = fid
        st.toast("已记录 👍，感谢反馈！")
        st.rerun()
    if c2.button("👎", key=f"down_{history_key}_{idx}"):
        fid = feedback_store.record(
            session_id, _last_user_prompt(history, idx), msg["content"], "down"
        )
        msg["feedback_id"] = fid
        st.session_state["pending_regen"] = {
            "history_key": history_key,
            "idx": idx,
            "session_id": session_id,
        }
        st.toast("已记录 👎，点击下方可重新生成")
        st.rerun()
    if msg.get("feedback_id"):
        st.caption("✓ 已反馈")


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

    # ========== 模型设置（provider 注册表 + 动态拉取模型，对齐 Hermes）==========
    st.markdown("### ⚙️ 模型设置")
    providers = list_providers()
    prov_names = [p.name for p in providers]
    prov_labels = {p.name: p.label for p in providers}
    cur_provider = st.session_state.llm_provider
    if cur_provider not in prov_names:
        cur_provider = prov_names[0]
    sel_provider = st.selectbox(
        "模型提供商",
        prov_names,
        index=prov_names.index(cur_provider),
        format_func=lambda n: prov_labels.get(n, n),
    )

    # 本地服务（Ollama）可改 base 地址；custom 可改 Base URL
    if sel_provider == "ollama":
        settings.LOCAL_MODEL_BASE_URL = st.text_input(
            "本地 API 地址", value=settings.LOCAL_MODEL_BASE_URL
        )
    elif sel_provider == "custom":
        settings.LLM_BASE_URL = st.text_input(
            "自定义 Base URL", value=settings.LLM_BASE_URL
        )

    # 模型选择：可手动输入，或「拉取可用模型」填充下拉
    if "model_options" not in st.session_state:
        st.session_state.model_options = []
    col_fetch, col_refresh = st.columns([3, 1])
    with col_fetch:
        model_input = st.text_input(
            "模型名（可手动输入，或点右侧拉取）",
            value=st.session_state.llm_model,
        )
    with col_refresh:
        if st.button("🔄 拉取", use_container_width=True):
            with st.spinner("拉取可用模型..."):
                opts = list_provider_models(
                    sel_provider,
                    base_url=(settings.LOCAL_MODEL_BASE_URL if sel_provider == "ollama" else None),
                )
            st.session_state.model_options = opts
            if opts:
                st.toast(f"已拉取 {len(opts)} 个模型")
            else:
                st.toast("未拉取到（可能无 key 或网络受限），可手动输入")
            st.rerun()

    if st.session_state.model_options:
        opts = list(st.session_state.model_options)
        if model_input and model_input not in opts:
            opts = [model_input] + opts
        sel_model = st.selectbox("选择模型", opts, index=0)
    else:
        sel_model = model_input

    if st.button("🔌 应用并测试连接", use_container_width=True):
        st.session_state.llm_provider = sel_provider
        st.session_state.llm_model = (
            sel_model
            or (get_provider(sel_provider).default_model if get_provider(sel_provider) else "")
        )
        init_agent.clear()
        agent = init_agent(sel_provider, st.session_state.llm_model)
        ok, msg = agent.llm.test_connection()
        st.session_state["conn_status"] = (ok, msg)
        st.rerun()
    if "conn_status" in st.session_state:
        ok, msg = st.session_state["conn_status"]
        if ok:
            st.success(f"🔗 {msg}")
        else:
            st.error(f"⚠️ {msg}")
    # 视觉能力指示（按当前实际模型）
    if agent.llm.supports_vision:
        st.caption(f"👁️ 当前模型 {agent.llm.model} 支持图片识别（多模态）")
    else:
        st.caption("🚫 当前模型不支持视觉；可在 .env 设 AUX_VISION_MODEL 走辅助视觉模型")

    # P0：权限 / 推理模式状态指示
    st.caption(f"🔐 权限模式：{settings.TOOL_PERMISSION_MODE}（ask=危险工具需授权）")
    st.caption(f"🧠 推理模式：{'开' if settings.REASONING_ENABLED else '关'}（REASONING_ENABLED）")

    st.divider()

    # ========== MCP 服务器（Phase 4）==========
    st.markdown("### 🔌 MCP 服务器")
    mcp_status = agent.mcp.status()
    if mcp_status:
        for s in mcp_status:
            st.caption(f"• {s['name']}：{len(s['tools'])} 个工具" + (" ✅" if s["connected"] else " ❌"))
    else:
        st.caption("未连接 MCP 服务器（在 .env 设置 MCP_SERVERS）")
    if st.button("🔌 连接 / 重载 MCP", use_container_width=True):
        with st.spinner("正在连接 MCP 服务器..."):
            res = agent.mcp.connect_all(timeout=10)
        st.session_state["mcp_res"] = res
        st.rerun()
    if "mcp_res" in st.session_state:
        n = sum(len(v) for v in st.session_state["mcp_res"].values())
        st.success(f"已注册 {n} 个 MCP 工具")

    st.divider()

    # ========== Skills Hub / 信任分级（Phase 5）==========
    st.markdown("### 🔐 技能信任 / Hub")
    trust_rows = agent.skill_evolver.trust_status()
    _trust_badge = {"trusted": "✅", "unverified": "⚠️", "quarantined": "🚫"}
    if trust_rows:
        for r in trust_rows:
            st.caption(f"{_trust_badge.get(r['trust'], '')} {r['name']} [{r['trust']}]")
    else:
        st.caption("暂无技能")
    hub_ref = st.text_input(
        "🌐 从 Hub 安装 (ref: local:/url:/github:)", key="hub_ref"
    )
    if st.button("📥 Hub 安装", use_container_width=True) and hub_ref.strip():
        with st.spinner("正在从 Hub 安装..."):
            from core.skills_evolution import skill_manage as _skill_manage
            res = _skill_manage(action="hub_install", name="", ref=hub_ref.strip())
        st.session_state["hub_res"] = res
        st.rerun()
    if "hub_res" in st.session_state:
        st.info(st.session_state["hub_res"])

    st.divider()

    # ========== RL 训练回路（Phase 5，Atropos 对齐）==========
    st.markdown("### 🎯 RL 训练回路")
    rl_st = agent.rl.status()
    st.caption(
        f"轨迹 {rl_st['total_trajectories']} · 已评分 {rl_st['scored']} · "
        f"平均奖励 {rl_st['mean_reward']} · 偏好对 {rl_st['preferences']}"
    )
    if st.button("📤 导出 Atropos 数据", use_container_width=True):
        with st.spinner("正在导出..."):
            res = agent.rl.export_atropos()
        st.session_state["rl_export"] = res
        st.rerun()
    if "rl_export" in st.session_state:
        ex = st.session_state["rl_export"]
        if ex.get("ok"):
            st.success(
                f"已导出到 {ex['dir']}（{ex['n_trajectories']} 条轨迹 / "
                f"{ex['n_preferences']} 偏好对）"
            )
            st.caption(ex.get("note", ""))
        else:
            st.error(str(ex))

    st.divider()

    # ========== 定时任务（P1，对齐 Hermes cron）==========
    st.markdown("### ⏰ 定时任务")
    sched = agent.scheduler.status()
    _sched_state = (
        "🟢 运行中" if sched["running"]
        else ("🔴 已禁用" if not sched["enabled"] else "⚪ 未启动")
    )
    st.caption(
        f"状态：{_sched_state} · tick {sched['tick_seconds']}s · 收件箱 {sched['inbox_count']}"
    )
    jobs = sched["jobs"]
    if jobs:
        for j in jobs:
            col_a, col_b = st.columns([3, 1])
            with col_a:
                st.caption(
                    f"• {j['name']} [{j['state']}] | {j['schedule']['display']}"
                    f" | 下次 {j['next_run_at'] or '—'}"
                )
            with col_b:
                if st.button("▶️ 运行", key=f"run_{j['id']}", use_container_width=True):
                    with st.spinner("执行中..."):
                        res = agent.scheduler.run_now(j["id"])
                    st.session_state["cron_res"] = res
                    st.rerun()
    else:
        st.caption("暂无定时任务（用 /cronjob 工具或下方创建）")

    with st.expander("➕ 新建定时任务"):
        cj_name = st.text_input("任务名", key="cj_name")
        cj_prompt = st.text_area("任务指令（需自包含）", key="cj_prompt")
        cj_sched = st.text_input(
            "调度", placeholder="0 9 * * * / every 2h / 30m / 2025-01-15T09:00:00",
            key="cj_sched",
        )
        cj_deliver = st.selectbox("投递", ["ui", "local"], key="cj_deliver")
        if st.button("📌 创建", use_container_width=True) and cj_name and cj_prompt and cj_sched:
            with st.spinner("创建中..."):
                res = agent.scheduler.create(cj_name, cj_prompt, cj_sched, deliver=cj_deliver)
            st.session_state["cron_create"] = res
            st.rerun()
    if "cron_create" in st.session_state:
        cc = st.session_state["cron_create"]
        if cc.get("ok"):
            st.success(f"✅ 已创建《{cc['name']}》，下次运行 {cc['next_run_at']}")
        else:
            st.error(str(cc))
    if "cron_res" in st.session_state:
        cr = st.session_state["cron_res"]
        if cr.get("ok"):
            st.info("✅ 已执行，结果已投递到收件箱")
        else:
            st.error(str(cr))
    if sched["inbox_count"]:
        with st.expander(f"📥 定时结果收件箱 ({sched['inbox_count']})"):
            for item in list(reversed(sched["inbox"]))[:10]:
                st.markdown(f"**{item.get('name')}** · {item.get('delivered_at')}")
                st.caption(item.get("content", "")[:400])

    st.divider()

    # ========== DPO 反馈数据导出（P2-3）==========
    st.markdown("### 📊 反馈数据")
    stats = feedback_store.stats()
    st.caption(
        f"👍 {stats['up']} · 👎 {stats['down']} · 已重生成 {stats['regenerated']}"
    )
    if st.button("📤 导出 DPO JSONL", use_container_width=True):
        out = str(settings.MEMORY_DIR / "dpo_feedback.jsonl")
        n = feedback_store.export_dpo_jsonl(out)
        st.session_state["dpo_path"] = out
        st.session_state["dpo_count"] = n
        st.rerun()
    if "dpo_path" in st.session_state:
        with open(st.session_state["dpo_path"], "r", encoding="utf-8") as f:
            st.download_button(
                "⬇️ 下载 DPO 数据",
                f.read(),
                file_name="dpo_feedback.jsonl",
                mime="application/jsonl",
            )
        st.caption(f"已导出 {st.session_state['dpo_count']} 条完整 DPO 对")

    st.divider()

    # ========== 自我进化（参照 Hermes 闭环）==========
    st.markdown("### 🧠 自我进化")
    learned = agent.skill_evolver.list_learned()
    st.caption(f"📚 已掌握可复用技能：{len(learned)} 个")
    if learned:
        with st.expander("查看已学技能"):
            for s in learned:
                trust = s.get("trust", "unverified")
                badge = {"trusted": "✅", "unverified": "⚠️", "quarantined": "🚫"}.get(trust, "")
                st.markdown(f"- {badge} **{s['title']}**：{s['description']} _[{trust}]_")
    if st.button("🧠 自我整理（更新画像+蒸馏）", use_container_width=True):
        sid = st.session_state.get("session_id") or st.session_state.get(
            "role_session_id", ""
        )
        with st.spinner("正在自我整理..."):
            summary = agent.run_nudge(sid)
        st.session_state["nudge_summary"] = summary
        st.rerun()
    if "nudge_summary" in st.session_state:
        st.success(st.session_state["nudge_summary"])

    st.divider()

    # ========== 事件钩子（P1b，对齐 Hermes Hooks）==========
    st.markdown("### ⚡ 事件钩子")
    hooks = agent.hooks.list()
    if hooks:
        for h in hooks:
            st.caption(
                f"• {h['name']} [{h['event']}] {h['action']}"
                f"{(' 匹配='+h['matcher']) if h.get('matcher') else ''}"
                f" {'🟢' if h.get('enabled', True) else '⚪'}"
            )
    else:
        st.caption("暂无钩子（钩子在生命周期点注入上下文/运行命令/否决工具）")
    with st.expander("➕ 添加钩子"):
        hk_name = st.text_input("钩子名", key="hk_name")
        hk_event = st.selectbox(
            "事件",
            ["session_start", "user_message", "pre_llm_call", "pre_tool_call",
             "post_tool_call", "assistant_message", "error"],
            key="hk_event",
        )
        hk_cmd = st.text_input("命令（action=command 时必填）", key="hk_cmd")
        hk_action = st.selectbox("动作", ["command", "inject"], key="hk_action")
        hk_matcher = st.text_input("匹配正则（仅 tool 事件，可空）", key="hk_matcher")
        if st.button("⚡ 添加", use_container_width=True) and hk_name:
            with st.spinner("添加钩子..."):
                res = agent.hooks.add(
                    hk_name, hk_event, command=hk_cmd or None, action=hk_action,
                    matcher=hk_matcher or None,
                )
            st.session_state["hk_res"] = res
            st.rerun()
    if "hk_res" in st.session_state:
        r = st.session_state["hk_res"]
        st.success(f"✅ {r.get('name')}") if r.get("ok") else st.error(str(r))

    st.divider()

    # ========== 斜杠命令（P1b，对齐 Hermes slash commands）==========
    st.markdown("### ⌨️ 斜杠命令")
    cmds = agent.commands.list()
    if cmds:
        for c in cmds:
            if st.button(f"/{c['name']}", key=f"cmd_{c['name']}", use_container_width=True):
                st.session_state["prefill"] = f"/{c['name']} "
                st.rerun()
    else:
        st.caption("暂无命令（在 commands/ 放 <name>.md，聊天框输入 /名称 即可展开）")
    with st.expander("➕ 新建命令"):
        nc_name = st.text_input("命令名（无空格）", key="nc_name")
        nc_desc = st.text_input("描述", key="nc_desc")
        nc_body = st.text_area("正文（用 $ARGUMENTS 占位参数）", key="nc_body")
        if st.button("📝 创建", use_container_width=True) and nc_name and nc_body:
            res = agent.commands.create(nc_name, nc_body, description=nc_desc)
            st.session_state["cmd_res"] = res
            st.rerun()
    if "cmd_res" in st.session_state:
        r = st.session_state["cmd_res"]
        st.success(f"✅ /{r.get('name')}") if r.get("ok") else st.error(str(r.get("error")))

    st.divider()

    # ========== 浏览器自动化（P1b）==========
    st.markdown("### 🌐 浏览器")
    _pw_ok = False
    try:
        import importlib.util
        _pw_ok = importlib.util.find_spec("playwright") is not None
    except Exception:
        _pw_ok = False
    _be = settings.BROWSER_BACKEND
    _mode = ("完整(Playwright)" if (_be == "playwright" or (_be == "auto" and _pw_ok))
             else ("仅网页读取(fetch)" if _be == "fetch" else ("网页读取(Playwright 未装)" if _be == "auto" else _be)))
    st.caption(f"后端：{_mode}（BROWSER_BACKEND={_be}）")
    st.caption("聊天中让小伴调用 browser 工具，或输入 / 命令")

    st.divider()

    # ========== 辩证用户画像（P1b，对齐 Honcho）==========
    st.markdown("### 👤 用户画像(辩证)")
    _profile = agent.user_modeler.get_profile()
    if _profile:
        with st.expander("查看当前画像"):
            st.markdown(_profile[:1500])
    else:
        st.caption("暂无画像（多在对话后点「自我整理」积累）")

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

    # 主动对话（P2-2）：一键让小伴开启话题
    if st.button("💡 主动聊点什么", use_container_width=False):
        ping = agent.proactive_ping(st.session_state.session_id)
        st.session_state.chat_history.append({"role": "assistant", "content": ping})
        st.rerun()

    # 首访欢迎语（仅当还没有任何对话）
    if not st.session_state.chat_history:
        with st.chat_message("assistant"):
            st.markdown(agent.greeting())

    # 处理 👎 后的重新生成（P2-3）
    if st.session_state.get("pending_regen", {}).get("history_key") == "chat_history":
        info = st.session_state.pop("pending_regen")
        hist = st.session_state.chat_history
        up = _last_user_prompt(hist, info["idx"])
        with st.chat_message("assistant"):
            new_reply = st.write_stream(agent.chat_stream(info["session_id"], up))
        hist.append({"role": "assistant", "content": new_reply})
        if hist[info["idx"]].get("feedback_id"):
            feedback_store.attach_regeneration(hist[info["idx"]]["feedback_id"], new_reply)
        st.rerun()

    # 显示聊天历史
    for i, msg in enumerate(st.session_state.chat_history):
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if msg["role"] == "assistant":
                render_assistant_feedback(
                    i, st.session_state.chat_history, st.session_state.session_id, "chat_history"
                )

    # 附件上传：图片（多模态视觉）/ 文件（文本上下文）
    plan_mode = st.checkbox("📋 计划模式（只规划、不执行任何工具）", value=False, key="plan_mode")
    moa_mode = st.checkbox(
        "🧩 多视角(MoA)回答：多个顾问并行分析 + 聚合者综合", value=False, key="moa_mode"
    )
    # MoA 真实多模型池（P1e）：每个顾问可用不同模型，模型差异提供多样性
    moa_advisor_models = st.text_input(
        "MoA 顾问模型池（provider:model，逗号分隔；留空=同模型多视角）",
        value=st.session_state.get("moa_advisor_models", settings.MOA_ADVISOR_MODELS),
        key="moa_advisor_models",
        help="例: deepseek:deepseek-chat, qwen:qwen-plus, openai:gpt-4o-mini。"
             "每个顾问用不同模型并行分析。",
    )
    moa_aggregator = st.text_input(
        "MoA 聚合者模型（provider:model；留空=主模型+工具）",
        value=st.session_state.get("moa_aggregator", settings.MOA_AGGREGATOR),
        key="moa_aggregator",
        help="留空则用主模型并保留完整工具能力；指定则单轮综合。",
    )
    c1, c2 = st.columns(2)
    with c1:
        imgs = st.file_uploader(
            "📎 图片（可多张，支持视觉识别）",
            type=["png", "jpg", "jpeg", "webp", "gif", "bmp"],
            accept_multiple_files=True, key="img_uploader",
        )
    with c2:
        ffs = st.file_uploader(
            "📎 文件（文本类，将作为上下文）",
            accept_multiple_files=True, key="file_uploader",
        )
    if imgs:
        st.session_state["pending_images"] = [
            "data:" + (im.type or "image/png") + ";base64," + base64.b64encode(im.getvalue()).decode()
            for im in imgs
        ]
        st.caption(f"📎 已附加 {len(imgs)} 张图片（发送时随消息一起识别）")
    if ffs:
        files_ctx = []
        for f in ffs:
            try:
                content = f.getvalue().decode("utf-8", errors="ignore")
            except Exception:
                content = "(无法以文本读取)"
            files_ctx.append({"name": f.name, "content": content[:8000]})
        st.session_state["pending_files"] = files_ctx
        st.caption(f"📎 已附加 {len(ffs)} 个文件（作为上下文注入）")

    # 输入框
    if prompt := st.chat_input("说说你的想法...（可附带上方图片/文件；输入 / 调用斜杠命令）"):
        # 支持侧栏按钮预填（以「/」开头的指令）
        if st.session_state.get("prefill"):
            prompt = st.session_state.pop("prefill") + prompt
        # 斜杠命令展开（对齐 Hermes slash commands）
        if prompt.startswith("/"):
            _parts = prompt[1:].split(" ", 1)
            _cmd = _parts[0]
            _args = _parts[1] if len(_parts) > 1 else ""
            _rendered = agent.commands.render(_cmd, _args)
            if _rendered:
                st.toast(f"⌨️ 已展开 /{_cmd}")
                prompt = _rendered
            else:
                st.warning(f"未找到斜杠命令：/{_cmd}")
        images = st.session_state.pop("pending_images", None)
        files = st.session_state.pop("pending_files", None)
        if actor.active:
            st.info(f"🎭 当前以 {actor.active} 模式回答", icon="🎭")

        st.session_state.chat_history.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)
            if images:
                st.caption(f"📎 {len(images)} 张图片")
            if files:
                st.caption("📎 " + "、".join(f["name"] for f in files))

        with st.chat_message("assistant"):
            if moa_mode and not (images or files):
                # P1e：Mixture-of-Agents 多视角回答（真实多模型池 / 同模型多视角）
                with st.spinner("🧩 MoA 多顾问并行分析中..."):
                    reply = agent.moa(
                        prompt,
                        advisor_specs=moa_advisor_models.strip() or None,
                        aggregator_spec=moa_aggregator.strip() or None,
                    )
                st.markdown(reply)
            else:
                # 流式输出（含工具调用状态 + 多模态图片/文件上下文）
                reply = st.write_stream(
                    agent.chat_stream(
                        st.session_state.session_id, prompt, images=images, files=files,
                        plan_mode=plan_mode,
                    )
                )

        # P0：若本轮触发了上下文压缩，提示用户
        if getattr(agent, "_last_compressed", False):
            st.caption("🗜️ 本轮对话已自动压缩过长历史以节省上下文")

        st.session_state.chat_history.append({"role": "assistant", "content": reply})

        # 自进化：轮后尝试把多步任务结晶为可复用技能（Hermes 式「grows with you」）
        skill = agent.evolve_after_turn(st.session_state.session_id, prompt, reply)
        if skill:
            st.toast(f"🧠 小伴学会了新技能《{skill['title']}》")

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

        for i, msg in enumerate(st.session_state.role_chat_history):
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])
                if msg["role"] == "assistant":
                    render_assistant_feedback(
                        i, st.session_state.role_chat_history,
                        st.session_state.role_session_id, "role_chat_history"
                    )

        # 处理 👎 后的重新生成（P2-3）
        if st.session_state.get("pending_regen", {}).get("history_key") == "role_chat_history":
            info = st.session_state.pop("pending_regen")
            hist = st.session_state.role_chat_history
            up = _last_user_prompt(hist, info["idx"])
            with st.chat_message("assistant", avatar="🎭"):
                new_reply = st.write_stream(agent.chat_stream(info["session_id"], up))
            hist.append({"role": "assistant", "content": new_reply})
            if hist[info["idx"]].get("feedback_id"):
                feedback_store.attach_regeneration(hist[info["idx"]]["feedback_id"], new_reply)
            st.rerun()

        if prompt := st.chat_input(f"对{p.name}说点什么..."):
            st.session_state.role_chat_history.append({"role": "user", "content": prompt})
            with st.chat_message("user"):
                st.markdown(prompt)

            with st.chat_message("assistant", avatar="🎭"):
                reply = st.write_stream(agent.chat_stream(st.session_state.role_session_id, prompt))

            st.session_state.role_chat_history.append({"role": "assistant", "content": reply})

            # 自进化：角色模式下也尝试结晶可复用技能
            skill = agent.evolve_after_turn(st.session_state.role_session_id, prompt, reply)
            if skill:
                st.toast(f"🧠 小伴学会了新技能《{skill['title']}》")
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