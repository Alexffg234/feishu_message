"""消息业务服务。

职责:接收飞书事件回调,解析出结构化消息,并通过 DAO 落库。
含防重(chat_id+message_id)与群白名单过滤。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.dao.message_repo import MessageRepo
from app.models.message import Message
from app.schemas.message import MessageCreate
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

logger = logging.getLogger(__name__)

# 支持解析的内容类型
_TEXT_TYPES = {"text", "post"}


class MessageService:
    def __init__(self, session: Session, allowed_chat_ids: set[str] | None = None):
        self._session = session
        self._allowed = allowed_chat_ids or set()
        self._repo = MessageRepo(session)

    # ---------------------------------------------------------------- 对外
    def handle_receive_event(self, data: P2ImMessageReceiveV1) -> bool:
        """处理一条 im.message.receive_v1 事件。返回是否成功入库。"""
        event = data.event
        if event is None or event.message is None:
            logger.warning("收到空消息事件,忽略")
            return False

        msg = event.message
        chat_id = msg.chat_id or ""

        # 群白名单过滤
        if self._allowed and chat_id not in self._allowed:
            logger.info("跳过非白名单群 chat_id=%s", chat_id)
            return False

        # 解析发送者
        sender_id, sender_type = _sender_info(event.sender)

        # 解析消息内容
        text, raw = _parse_content(msg.message_type or "", msg.content or "")

        create = MessageCreate(
            message_id=msg.message_id or "",
            chat_id=chat_id,
            sender_id=sender_id,
            sender_type=sender_type,
            message_type=msg.message_type or "",
            text=text,
            create_time=getattr(msg, "create_time", None) or "",
            raw=raw,
        )

        return self._save(create)

    # ---------------------------------------------------------------- 内部
    def _save(self, create: MessageCreate) -> bool:
        """落库并防重。返回 True 表示新写入,False 表示重复跳过。"""
        # 防重:同一 chat+message 不重复入库
        if self._repo.exists(create.chat_id, create.message_id):
            logger.debug("重复消息跳过 %s/%s", create.chat_id, create.message_id)
            return False

        msg = Message(
            message_id=create.message_id,
            chat_id=create.chat_id,
            sender_id=create.sender_id,
            sender_type=create.sender_type,
            message_type=create.message_type,
            text=create.text,
            create_time=create.create_time,
            raw=create.raw,
        )
        try:
            self._repo.insert(msg)
            logger.info(
                "消息入库 [%s] %s @ %s (%s): %s",
                create.message_id,
                create.sender_id,
                create.chat_id,
                create.message_type,
                create.text[:80],
            )
            return True
        except Exception:  # noqa: BLE001  (唯一索引竞态等)
            self._session.rollback()
            # 竞态导致的重复,直接忽略
            if self._repo.exists(create.chat_id, create.message_id):
                logger.debug("并发重复消息,忽略")
                return False
            logger.exception("消息入库失败 %r", create)
            raise


# ---------------------------------------------------------------- 解析工具
def _sender_info(sender: Any) -> tuple[str, str]:
    if sender is None:
        return "", ""
    s = getattr(sender, "sender_id", None)
    sid = ""
    if s is not None:
        sid = (
            getattr(s, "open_id", None)
            or getattr(s, "user_id", None)
            or getattr(s, "union_id", None)
            or ""
        )
    stype = getattr(sender, "sender_type", None) or ""
    return sid, stype


def _parse_content(msg_type: str, content_json: str) -> tuple[str, dict[str, Any] | None]:
    """解析 content 为 (可读文本, 原始结构化数据)。

    支持 text / post(富文本)。图片/文件/语音等保留原始数据,文本为空。
    """
    try:
        raw: Any = json.loads(content_json) if content_json else {}
    except json.JSONDecodeError:
        logger.warning("消息 content 非合法 JSON: %r", content_json[:200])
        return "", {"raw_content": content_json}

    if not isinstance(raw, dict):
        return "", {"raw_content": content_json}

    if msg_type == "text":
        return (raw.get("text") or "").strip(), raw
    if msg_type == "post":
        text = _extract_post_text(raw.get("content"))
        return text, raw
    return "", raw


def _extract_post_text(content: Any) -> str:
    """从飞书富文本 post 提取纯文本。"""
    parts: list[str] = []
    if isinstance(content, list):
        for section in content:
            if isinstance(section, list):
                for run in section:
                    if isinstance(run, dict) and run.get("text"):
                        parts.append(run["text"])
    return " ".join(parts).strip()
