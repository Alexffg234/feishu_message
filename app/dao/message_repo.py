"""消息数据访问层 (DAO)。

封装对 messages 表的读写,业务层不直接接触 SQLAlchemy 细节。
"""
from __future__ import annotations

from sqlalchemy import func, select
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

    def get_by_message_id(self, chat_id: str, message_id: str) -> Message | None:
        stmt = select(Message).where(
            Message.chat_id == chat_id, Message.message_id == message_id
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def list_roots(
        self, chat_id: str | None = None, limit: int = 50
    ) -> list[tuple[str, str]]:
        """最近 N 个分组,返回 [(root_message_id, chat_id), ...]。

        按每组内最新一条消息的时间倒序排列。
        """
        last_id = func.max(Message.id).label("last_id")
        stmt = (
            select(Message.root_message_id, Message.chat_id, last_id)
            .where(Message.root_message_id != "")
            .group_by(Message.root_message_id, Message.chat_id)
            .order_by(last_id.desc())
            .limit(limit)
        )
        if chat_id:
            stmt = stmt.where(Message.chat_id == chat_id)
        rows = self._session.execute(stmt).all()
        return [(r.root_message_id, r.chat_id) for r in rows]

    def list_by_root(
        self, root_message_id: str, chat_id: str | None = None
    ) -> list[Message]:
        """取同一分组(同一 root)下的全部消息,按时间正序。"""
        stmt = select(Message).where(Message.root_message_id == root_message_id)
        if chat_id:
            stmt = stmt.where(Message.chat_id == chat_id)
        stmt = stmt.order_by(Message.received_at.asc(), Message.id.asc())
        return list(self._session.scalars(stmt).all())

    # ---------------------------------------------------------------- topic
    def group_has_topic(self, chat_id: str, root_message_id: str) -> bool:
        stmt = (
            select(Message.id)
            .where(
                Message.chat_id == chat_id,
                Message.root_message_id == root_message_id,
                Message.topic != "",
            )
            .limit(1)
        )
        return self._session.execute(stmt).first() is not None

    def set_topic_for_group(
        self, chat_id: str, root_message_id: str, topic: str
    ) -> int:
        """整组(同一回复链)统一打 topic 标签,返回影响行数。"""
        from sqlalchemy import update

        stmt = (
            update(Message)
            .where(
                Message.chat_id == chat_id,
                Message.root_message_id == root_message_id,
            )
            .values(topic=topic)
        )
        return self._session.execute(stmt).rowcount or 0

    def get_existing_topics(self, limit: int = 100) -> list[str]:
        stmt = (
            select(Message.topic)
            .where(Message.topic != "")
            .distinct()
            .order_by(Message.topic)
            .limit(limit)
        )
        return [r[0] for r in self._session.execute(stmt).all()]

    def list_topics(
        self, chat_id: str | None = None, limit: int = 50
    ) -> list[tuple[str, int]]:
        """最近活跃的 N 个主题,返回 [(topic, 组内最新消息 id), ...]。"""
        last_id = func.max(Message.id).label("last_id")
        stmt = (
            select(Message.topic, last_id)
            .where(Message.topic != "")
            .group_by(Message.topic)
            .order_by(last_id.desc())
            .limit(limit)
        )
        if chat_id:
            stmt = stmt.where(Message.chat_id == chat_id)
        rows = self._session.execute(stmt).all()
        return [(r.topic, r.last_id) for r in rows]

    def list_by_topic(
        self, topic: str, chat_id: str | None = None, limit: int = 1000
    ) -> list[Message]:
        stmt = select(Message).where(Message.topic == topic)
        if chat_id:
            stmt = stmt.where(Message.chat_id == chat_id)
        stmt = stmt.order_by(Message.received_at.asc(), Message.id.asc()).limit(limit)
        return list(self._session.scalars(stmt).all())
