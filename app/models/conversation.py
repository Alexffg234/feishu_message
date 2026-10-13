"""对话关系存储模型。

对话关系作为一等公民独立建模,支持任意深度的多层级回复树:
- conversations   一条回复链 = 一个对话(thread),存对话级聚合信息
- message_links   每条「回复」边 child→parent,完整保留树形结构(A→B→C→D)

与 messages 的关系:
- messages 表存消息详情,新增 conversation_id 外键挂到对话
- message_links 记录回复关系,parent_message_id / child_message_id 指向 messages.message_id

设计约定:
- 独立消息(无人回复)也自成一条对话(root_message_id = 自身 message_id)便于统一查询
- depth 表示该子消息在树中的深度:根消息 depth=0,直接回复根的消息 depth=1,以此类推
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.message import BigIntPK, Base, _utcnow


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # 该对话(回复链)的根消息 id;独立消息 root = 自身 message_id
    root_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    root_sender_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    # open=进行中 / closed=已结束(超时未回复)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open", index=True)
    # LLM 主题标签(沿用 messages.topic 语义),空=未分类
    topic: Mapped[str] = mapped_column(String(128), nullable=False, default="", index=True)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    participant_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # 时间(毫秒时间戳,与 messages.create_time 同源)
    started_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, index=True)
    last_message_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    # 同一群内一个回复链只允许一条对话记录
    __table_args__ = (
        UniqueConstraint("chat_id", "root_message_id", name="uq_conversation_chat_root"),
        Index("ix_conversations_updated_at", "updated_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Conversation {self.chat_id}/{self.root_message_id} topic={self.topic!r}>"


class MessageLink(Base):
    __tablename__ = "message_links"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    # 被回复的消息 id(父) / 回复消息 id(子),指向 messages.message_id
    parent_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    child_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    parent_sender_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    child_sender_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    # 该子消息在树中的深度(根=0)。直接回复根的消息 depth=1
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("parent_message_id", "child_message_id", name="uq_link_parent_child"),
        Index("ix_message_links_child", "child_message_id"),
        Index("ix_message_links_conversation_depth", "conversation_id", "depth"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<MessageLink {self.child_message_id} -> {self.parent_message_id} d={self.depth}>"
