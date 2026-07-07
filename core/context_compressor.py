"""上下文压缩（P0）

对齐 Hermes 的 `context_compressor` / `conversation_compression`：当对话历史超过
token 阈值时，把旧轮次摘要为一条 [历史对话摘要] 系统消息，保留最近 keep_recent 轮。

设计原则：**非破坏式**——只处理「即将发给模型的消息列表」副本，不改写持久记忆
（memory 中仍保留完整历史，便于检索/蒸馏）。
"""

import logging

from config import settings

logger = logging.getLogger(__name__)


def estimate_tokens(messages: list[dict]) -> int:
    """粗略估算消息列表的 token 数（中文约 1 字 1 token，英文约 4 字符 1 token）。"""
    n = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            n += len(c)
        elif isinstance(c, list):  # 多模态 content 块
            for part in c:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    n += len(part["text"])
                else:
                    n += 200
        n += 8  # role/结构开销
    return int(n / 1.6)


def _render(messages: list[dict]) -> list[str]:
    out = []
    for m in messages:
        role = m.get("role", "?")
        c = m.get("content")
        if isinstance(c, list):
            c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
        out.append(f"{role}: {c}")
    return out


def _summarize(llm, to_summarize: list[dict]) -> str:
    try:
        transcript = _render(to_summarize)
        prompt = (
            "请把以下对话历史压缩为简洁的结构化摘要：用要点列出关键事实、用户偏好、"
            "待办事项与未完成结论，保留人物/时间/数字等关键信息，不超过 400 字：\n\n"
            + "\n".join(transcript)
        )
        resp = llm.chat(
            messages=[{"role": "user", "content": prompt}], temperature=0.2, max_tokens=400
        )
        return resp.choices[0].message.content or ""
    except Exception as e:
        logger.warning(f"上下文压缩摘要失败：{e}")
        return "(历史摘要生成失败，已跳过压缩)"


def compress_if_needed(
    messages: list[dict], llm, threshold: int | None = None, keep_recent: int = 6
) -> tuple[list[dict], bool]:
    """若总 token 超阈值则压缩。返回 (可能压缩后的消息, 是否发生了压缩)。"""
    threshold = threshold or getattr(settings, "CONTEXT_COMPACT_THRESHOLD", 6000)
    if estimate_tokens(messages) <= threshold:
        return messages, False

    sys_msgs = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]
    if len(rest) <= keep_recent:
        return messages, False

    to_summarize = rest[:-keep_recent]
    keep = rest[-keep_recent:]
    summary_text = _summarize(llm, to_summarize)
    summary_msg = {"role": "system", "content": f"[历史对话摘要]\n{summary_text}"}
    return sys_msgs + [summary_msg] + keep, True
