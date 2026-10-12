"""消息 ORM 模型。

表 messages 存储采集到的群聊消息。chat_id + message_id 加唯一索引防止重复入库。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import JSON, BigInteger, DateTime, Index, Integer, String, Text
from sqlalchemy.types import TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# SQLite 不支持 BigInteger 主键自增,这里让 sqlite 退回 Integer(自增),
# PostgreSQL / MySQL 仍用 BigInteger(8 字节,足够大)。
BigIntPK = BigInteger().with_variant(Integer, "sqlite")


class JSONText(TypeDecorator):
    """跨库 JSON 存取。

    PostgreSQL/SQLite 底层用原生 JSON;MySQL/MariaDB(5.5 无原生 JSON)
    底层用 TEXT,但读写时自动做 json 序列化,对上层完全透明。
    """

    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql" or dialect.name == "sqlite":
            return dialect.type_descriptor(JSON())
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if dialect.name in ("postgresql", "sqlite"):
            return value  # 交给原生 JSON 处理
        return json.dumps(value, ensure_ascii=False)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if dialect.name in ("postgresql", "sqlite"):
            return value
        if isinstance(value, str):
            try:
                return json.loads(value)
            except (TypeError, ValueError):
                return {"raw": value}
        return value


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    chat_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    sender_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    sender_type: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    message_type: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    create_time: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # 原始结构化数据(图片/文件等附加字段)
    raw: Mapped[dict | None] = mapped_column(JSONText, nullable=True)
    # 落库时间(UTC)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    # 同一 chat + 飞书 message_id 唯一,防重
    __table_args__ = (
        Index("ix_messages_chat_msg", "chat_id", "message_id", unique=True),
        Index("ix_messages_received_at", "received_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Message {self.message_id} {self.message_type} "
            f"chat={self.chat_id} sender={self.sender_id}>"
        )
