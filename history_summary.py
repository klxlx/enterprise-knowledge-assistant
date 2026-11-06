"""会话历史的截断与摘要压缩。

历史太长时，只把最近几轮原文交给模型，更早的对话用便宜的小模型压缩成摘要，
既保留上下文连续性，又避免每次请求都带上整段历史。
"""

from functools import lru_cache

from langchain_community.chat_models.tongyi import ChatTongyi
from langchain_core.messages import BaseMessage

import config_data as config
from file_history_store import load_summary, save_summary

SUMMARY_PROMPT = (
    "你是客服系统的记录员。请把下面的对话压缩成简洁的中文摘要，"
    "保留用户提到的关键信息、已确认的事实、结论和尚未解决的问题；"
    "不要添加原文里没有的信息，也不要写客套话。\n\n"
    "已有摘要：\n{previous}\n\n"
    "需要合并进摘要的新对话：\n{transcript}\n\n"
    "直接输出合并后的摘要正文。"
)


@lru_cache(maxsize=1)
def _summary_model() -> ChatTongyi:
    return ChatTongyi(model=config.summary_model_name)


def _render(messages: list[BaseMessage]) -> str:
    lines = []
    for message in messages:
        role = "用户" if message.type == "human" else "客服"
        lines.append(f"{role}：{message.content}")
    return "\n".join(lines)


def summarize(previous: str, messages: list[BaseMessage]) -> str:
    prompt = SUMMARY_PROMPT.format(
        previous=previous or "（无）",
        transcript=_render(messages),
    )
    response = _summary_model().invoke(prompt)
    return str(response.content).strip()


def prepare_history(
    session_id: str, messages: list[BaseMessage]
) -> tuple[str, list[BaseMessage]]:
    """返回（摘要文本, 最近几轮消息）。历史还短时摘要为空、消息原样返回。"""
    keep = max(1, config.history_keep_turns) * 2  # 一轮 = 用户 + 客服两条消息
    if len(messages) <= config.history_summary_after_messages:
        return "", messages

    boundary = len(messages) - keep
    record = load_summary(session_id)
    covered = int(record.get("covered_messages") or 0)
    summary_text = str(record.get("summary") or "")

    if boundary > covered:
        pending = messages[covered:boundary]
        # 攒够一批再调用摘要模型，避免每来一条消息就重新压缩一次。
        if len(pending) >= config.history_summary_trigger_messages:
            summary_text = summarize(summary_text, pending)
            save_summary(session_id, summary_text, boundary)

    return summary_text, messages[-keep:]
