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

    def list_topic_roots(self, chat_id: str, topic: str) -> list[tuple[str, int]]:
        """同群同 topic 的所有分组,返回 [(root_message_id, first_message_id)]。

        按最早消息 id 升序(最旧组在前),供同主题归组决定归并方向。
        """
        first_id = func.min(Message.id).label("first_id")
        stmt = (
            select(Message.root_message_id, first_id)
            .where(
                Message.chat_id == chat_id,
                Message.topic == topic,
                Message.root_message_id != "",
            )
            .group_by(Message.root_message_id)
            .order_by(first_id.asc())
        )
        return [(r[0], int(r[1])) for r in self._session.execute(stmt).all()]

    def list_all_topic_groups(
        self, chat_id: str | None = None, limit: int = 100000
    ) -> list[tuple[str, str, str, int]]:
        """所有已打标签的组,返回 [(chat_id, topic, root, first_id)] 按最早消息升序。

        供归并回填脚本按 (chat, topic) 规划归并对。
        """
        first_id = func.min(Message.id).label("first_id")
        stmt = (
            select(Message.chat_id, Message.topic, Message.root_message_id, first_id)
            .where(Message.topic != "", Message.root_message_id != "")
            .group_by(Message.chat_id, Message.topic, Message.root_message_id)
            .order_by(first_id.asc())
            .limit(limit)
        )
        if chat_id:
            stmt = stmt.where(Message.chat_id == chat_id)
        return [
            (r[0], r[1], r[2], int(r[3]))
            for r in self._session.execute(stmt).all()
        ]

    def get_topic_samples(
        self, chat_id: str | None = None, limit: int = 50
    ) -> list[tuple[str, str]]:
        """每个已有主题取最新一条消息文本作摘要,返回 [(topic, text)]。

        分类提示词用它让 LLM 判断「是否同一问题」,优先同群(chat_id)。
        """
        stmt = (
            select(Message.topic, Message.text, Message.id)
            .where(Message.topic != "")
            .order_by(Message.id.desc())
            .limit(2000)
        )
        if chat_id:
            stmt = stmt.where(Message.chat_id == chat_id)
        samples: dict[str, str] = {}
        for topic, text, _mid in self._session.execute(stmt):
            if topic not in samples:
                samples[topic] = (text or "").strip()[:50]
            if len(samples) >= limit:
                break
        return list(samples.items())

    def list_recent_before(
        self, chat_id: str, before_id: int, limit: int = 6
    ) -> list:
        """同群内、id 小于 before_id 的最近 limit 条消息,按时间升序。

        主题分类时作上下文:让 LLM 判断当前组是否延续前面讨论。
        """
        stmt = (
            select(Message)
            .where(Message.chat_id == chat_id, Message.id < before_id)
            .order_by(Message.id.desc())
            .limit(limit)
        )
        return list(reversed(self._session.execute(stmt).scalars().all()))

    def list_topic_openings(
        self, chat_id: str | None = None, limit: int = 100
    ) -> list[tuple[str, str]]:
        """每个主题取最早一条有信息量的消息作样本,返回 [(topic, text)]。

        归并判定时用案例开场消息比最新消息(常是「收到」类)更有代表性;
        过短文本(<6字)跳过。
        """
        stmt = (
            select(Message.topic, Message.text, Message.id)
            .where(Message.topic != "")
            .order_by(Message.id.asc())
            .limit(5000)
        )
        if chat_id:
            stmt = stmt.where(Message.chat_id == chat_id)
        samples: dict[str, str] = {}
        for topic, text, _mid in self._session.execute(stmt):
            t = (text or "").strip()
            if topic not in samples and len(t) >= 6:
                samples[topic] = t[:50]
            if len(samples) >= limit:
                break
        return list(samples.items())
