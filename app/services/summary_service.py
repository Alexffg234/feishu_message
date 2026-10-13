"""对话总结服务。

对一个「对话」(conversation,即一条回复链)调用 OpenAI 兼容 LLM,
生成一段简短的自然语言总结,用于可视化页面验证分类/总结效果。

- 复用 LLMClient 走 /chat/completions,无额外依赖
- 服务器配了 LLM_* 即可用;未配置时返回 disabled 状态,页面展示占位
- 总结不落库:每次查看实时生成(当前以简单为主)
"""
from __future__ import annotations

import logging
from functools import lru_cache

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.dao.conversation_repo import ConversationRepo
from app.services.llm import LLMClient, LLMError

logger = logging.getLogger(__name__)

_SUMMARY_SYSTEM = (
    "你是群聊总结助手。请用简体中文把下面这段对话总结成一小段话。\n"
    "要求:\n"
    "1. 概括这段对话讨论的主题、关键问题和结论,不超过 150 字。\n"
    "2. 直接输出总结内容,不要出现'总结:''以下是'等前缀,不要引用原文。"
)

_TOPIC_PROMPT = (
    "请给这段群聊对话起一个 2~10 字的中文主题标签(如:投屏、蓝牙连接、会议纪要)。\n"
    "只输出标签本身,不要引号、标点或解释。"
)


def _fmt_rows(rows: list) -> str:
    """把消息格式化为 LLM 可读的对话文本。"""
    lines = []
    for m in rows:
        text = (m.text or "").strip() or f"({m.message_type or '非文本'})"
        lines.append(f"{m.sender_id or '?'}: {text}")
    return "\n".join(lines)


class SummaryService:
    def __init__(self, llm: LLMClient | None = None):
        self._llm = llm

    @property
    def enabled(self) -> bool:
        return self._llm is not None

    def summarize(self, conversation_id: int) -> dict:
        """生成某对话的总结。返回 {enabled, summary, topic, conversation_id}。

        LLM 未配置/调用失败时 summary 为空字符串,由页面友好提示。
        """
        base = {
            "conversation_id": conversation_id,
            "enabled": self.enabled,
            "summary": "",
            "topic": "",
        }
        if not self.enabled:
            return base

        with SessionLocal() as session:
            from app.models.conversation import Conversation

            conv = session.get(Conversation, conversation_id)
            base["topic"] = conv.topic if conv else ""
            repo = ConversationRepo(session)
            tree = repo.list_tree(conversation_id)
            roots = tree["roots"]

        messages = [m for r in roots for m in _collect(r)]
        if not messages:
            return base

        conv_text = _fmt_rows(messages)
        try:
            base["summary"] = self._llm.chat(_SUMMARY_SYSTEM, conv_text)
        except LLMError as e:
            logger.warning("总结生成失败 conversation=%s: %s", conversation_id, e)
            base["summary"] = ""
        return base

    def suggest_topic(self, conversation_id: int) -> str:
        """(可选)为对话生成主题标签,失败返回空。"""
        if not self.enabled:
            return ""
        with SessionLocal() as session:
            repo = ConversationRepo(session)
            messages = [m for r in repo.list_tree(conversation_id)["roots"] for m in _collect(r)]
        if not messages:
            return ""
        try:
            return (self._llm.chat(_TOPIC_PROMPT, _fmt_rows(messages)) or "").strip()
        except LLMError:
            return ""


def _collect(node: dict) -> list:
    """深度优先展平回复树节点为消息列表(每条回复都加入)。"""
    out = []
    if node.get("message") is not None:
        out.append(node["message"])
    for c in node.get("children", []):
        out.extend(_collect(c))
    return out


@lru_cache
def get_summary_service() -> SummaryService:
    """应用级单例。按配置决定是否启用 LLM。"""
    settings = get_settings()
    if not settings.llm_enabled:
        logger.info("LLM 总结未配置(LLM_API_KEY/LLM_MODEL),已禁用")
        return SummaryService(llm=None)
    llm = LLMClient(
        settings.llm_api_base,
        settings.llm_api_key,
        settings.llm_model,
        settings.llm_timeout,
    )
    return SummaryService(llm=llm)
