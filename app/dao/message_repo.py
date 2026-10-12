"""消息数据访问层 (DAO)。

封装对 messages 表的读写,业务层不直接接触 SQLAlchemy 细节。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.message import Message


class MessageRepo:
    """Message 表的存取操作。"""

    def __init__(self, session: Session):
        self._session = session

    def insert(self, msg: Message) -> Message:
        """新增一条消息,返回对象。"""
        self._session.add(msg)
        self._session.commit()
        self._session.refresh(msg)
        return msg

    def insert_many(self, msgs: list[Message]) -> None:
        """批量新增(用于回放/补拉)。"""
        if not msgs:
            return
        self._session.add_all(msgs)
        self._session.commit()

    def exists(self, chat_id: str, message_id: str) -> bool:
        stmt = select(Message.id).where(
            Message.chat_id == chat_id, Message.message_id == message_id
        )
        return self._session.execute(stmt).first() is not None

    def count(self) -> int:
        stmt = select(Message.id)
        return len(self._session.execute(stmt).all())

    def list_recent(self, limit: int = 100) -> list[Message]:
        stmt = (
            select(Message)
            .order_by(Message.received_at.desc(), Message.id.desc())
            .limit(limit)
        )
        return list(self._session.scalars(stmt).all())

    def list_by_chat(self, chat_id: str, limit: int = 100) -> list[Message]:
        stmt = (
            select(Message)
            .where(Message.chat_id == chat_id)
            .order_by(Message.received_at.desc(), Message.id.desc())
            .limit(limit)
        )
        return list(self._session.scalars(stmt).all())
